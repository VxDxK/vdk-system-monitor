# Native artifact builds and selection

Python runs on the controller. Native tools are built for the device, independently
of whether it is reached through Host, SSH, or a future ADB/HDC backend.

## Build configuration

Run from a source checkout using Python 3.14+ and the pinned Rust toolchain:

```bash
uv run python scripts/build_artifacts.py
uv run python scripts/build_artifacts.py --target aarch64-unknown-linux-musl
uv run python scripts/build_artifacts.py --target x86_64-unknown-linux-gnu --tool rfpoll
uv run python scripts/build_artifacts.py --all
```

`--target` and `--tool` are repeatable; `--all` and `--target` are mutually
exclusive. No selection builds the configured default target and all tools.
`--config PATH` supplies another TOML configuration; `--output PATH` supplies a
separate bundle directory. Unknown tools/targets and missing builders fail before
any builds start. Container-backed selections also check engine access before
building any target. A build failure does not switch builders.

`scripts/build-targets.toml` declares the selected builder (`cargo` or `cross`),
linkage, execution profiles, and Rust flags. An optional target-level `tools`
list restricts the global tool list for that target. Explicit tool selections
must be available on every selected target. `Cross.toml` pins versioned images;
the script explicitly passes its location through `CROSS_CONFIG`. To use a
locally installed musl linker/toolchain, change the selected builder to `cargo`
in a custom configuration. Additional platform triples require extending the
packager and compatibility model as well as configuring a builder.

The build controller currently requires Unix for the publication lock. For the
musl entries, install cross 0.2.5 and provide Docker or Podman with permission to
run containers and pull the images:

```bash
cargo install cross --version 0.2.5 --locked
```

The script checks `CROSS_CONTAINER_ENGINE` when set; otherwise it selects Docker
first, then Podman, and passes that selection to cross. `docker info` (or
`podman info`) must succeed in the same session running the build. Permission
errors connecting to `/var/run/docker.sock` mean the engine is inaccessible;
installing cross alone is insufficient. In a sandbox, host Docker access may not
be exposed. Run container builds in a session with engine access, or explicitly
select `builder = "cargo"` in a custom configuration after installing the target
standard library and linker. The script does not change daemon/socket permissions.
Compiler/container build failures identify the failed target and leave the bundle
index unchanged, without a Python subprocess traceback.

The container images and target standard libraries may require network access.
Cargo builds require the pinned Rust toolchain and a suitable local linker.
Both builders use release mode, `--locked`, an explicit target, and the selected
binaries. Local Cargo output is under `binary-utils/target/<triple>/release/`.
Cross uses a separate target directory under
`binary-utils/target/cross/<triple>/<environment-key>/`, with executables in its
`<triple>/release/` subdirectory. The key includes compiler identity, cross
version, Cross.toml content, and image environment overrides. This isolates host
procedural macros/build scripts from those loaded inside containers and prevents
reuse across different cross images. An explicit `--target` alone is insufficient:
Cargo still writes host proc-macro shared libraries under `<target-dir>/release/`.

Inherited `RUSTFLAGS` are replaced by explicit baseline CPU settings and the
configured flags. Custom flags enabling extra CPU features must declare matching
`cpu_features` in that target entry; these are asserted build requirements, not
features inferred from machine code. The default entries use baseline CPUs.

| Configured target | Builder | Execution requirement | Runtime verification |
|---|---|---|---|
| `x86_64-unknown-linux-gnu` | cargo | Linux, matching loader/glibc and shared libraries | Local native tests and Host/SSH-adapter tests; live SSH acceptance separate |
| `x86_64-unknown-linux-musl` | cross | x86-64 Linux kernel, fully static image | Local Linux CLI/file-collector checks; these do not establish hardware or live SSH support |
| `aarch64-unknown-linux-musl` | cross | AArch64 Linux kernel, fully static image | Build/header inspection only; device/emulator runtime acceptance pending |

The musl entries declare Linux, Android, and OpenHarmony execution profiles as
candidates. Android and OpenHarmony device support remains provisional. This
project does not provide ADB/HDC connections. Linux executables do not support LiteOS.
Android/OHOS-specific builds, AArch64 GNU, and non-Linux ports are deferred until
required and verified. Successful compilation does not establish procfs/sysfs,
netlink, network, signal, or measurement support on a device.

## Bundle contract

The generated `artifacts/manifest.json` has no format revision tag. Each tool maps to a list
of variants. Executables live at `<triple>/<tool>-<sha256>.bin`. Updating a subset
preserves other targets and tools. All selected builds and inspections must
succeed before publication. A writer lock serializes index updates; immutable
content-addressed files are published before atomic manifest replacement, keeping
the previous index valid if publication is interrupted. Old unreferenced files
may remain until `just clean`.

Each variant records its relative file location, SHA-256, Cargo package version,
Rust triple, ELF format, architecture, bitness, byte order, kernel family,
execution profiles, ABI, linkage, interpreter, required shared libraries, minimum
referenced glibc version, declared CPU features, and optional kernel/API minima.
Build provenance includes the Rust compiler, native source/lock/config hashes, builder, cross
version where applicable, and effective Rust flags. `verification: build-only`
is deliberately separate from device runtime evidence. A null kernel/API minimum
means no minimum was established by this packaging step; it is not a universal
compatibility guarantee. Null interpreter/glibc for static musl is inapplicable.

Versions come from Cargo metadata. ELF inspection uses Python's standard library
and never executes a foreign binary or requires `readelf`. Packaging checks CPU
architecture and expected linkage, rejects dynamically linked musl artifacts,
and checks that GNU images have a glibc dependency and interpreter. It does not
certify all machine instructions, kernel syscall requirements, or libc provenance.

Only the current multi-variant manifest is supported. Build bundles with the
matching controller release.

## Python defaults and explicit configuration

```python
from pathlib import Path
from vsm.binary import BinaryPreference, BinaryProvider, TargetProfile
from vsm.runtime import Run

# Automatically uses the imported source checkout's artifacts directory.
run = Run(Path("runs/example"))

# Equivalent explicit default provider.
run = Run(Path("runs/example"), binaries=BinaryProvider())

# An explicit bundle takes precedence; installed wheels need this form.
run = Run(Path("runs/example"), binaries=BinaryProvider(Path("/opt/vsm/artifacts")))

# Force this tool's compatible GNU variant rather than preferring static musl.
provider = BinaryProvider(preferences=(
    BinaryPreference(tool="rfpoll", target="x86_64-unknown-linux-gnu"),
))

# Supplement an unknown kernel family for a separately identified Android device.
# Apply this provider only to connections for which these facts are correct.
provider = BinaryProvider(target_profile=TargetProfile(kernel_family="linux"))
```

`Run(..., binaries=None)` also creates a default provider. Lookup is lazy: import,
run construction, and workflows without native tools do not require artifacts.
The default path is anchored to the imported source tree, with project metadata
and native-source layout checks; it is independent of the current working
directory and does not require `.git`. Outside a checkout, configure an explicit
bundle. Missing artifacts produce an error with the expected manifest location
and `just build-artifacts`. Wheels do not contain executable bundles, and runtime
never downloads or builds them.

Selection normalizes `arm64`/`aarch64` and `amd64`/`x86_64`. `TargetInfo.os`
identifies the platform; `kernel_family` distinguishes Linux from other kernels.
For Linux targets, `os="linux"` implies the Linux kernel family.
Android/OpenHarmony require explicit Linux-kernel evidence. Optional CPU, API,
loader, and library facts allow stricter matching. `TargetProfile` supplements
unknown facts and rejects conflicts with detected values, including libc.
Transport identity never supplies a Rust triple.

Selection checks the requested tool, platform, architecture, kernel and any
specified CPU/version requirements. Dynamic GNU artifacts additionally require
glibc with a sufficient version and compatible known loader/library facts.
A static musl artifact does not require musl on the device: it can be a candidate
on Bionic or an unknown libc when the other execution requirements match.
Compatible static musl is preferred, followed by compatible dynamic GNU. Equal
rank candidates are ambiguous and require bundle cleanup or an explicit target
preference; a preference never bypasses validation.

Before copying, the provider checks the hash, executable headers, linkage, and
manifest agreement. Paths must stay inside the bundle; symlink escapes and
symlink executable files are rejected. Deployment retains per-device, workspace,
and content reuse and verifies the target copy. For dynamic images the actual
target loader runs `--list` on the staged executable with a timeout, checking
library/version resolution without invoking the tool's main function. Failure
is explicit; it does not trigger a fallback to another variant. Static deployment
does not execute a startup probe; collection readiness remains the action's job.

Real-device acceptance must cover deployment, startup, sampling, clean shutdown,
and strict processing per tool. A successful version command is insufficient.
Device security policy, execution locations, procfs visibility, privileges, and
kernel features can prevent a compatible image from providing a measurement.

For a local build using a custom Cargo configuration, install the corresponding
Rust target first (for example, `rustup target add x86_64-unknown-linux-musl`).
The rup ancillary-message handling supports both musl's 32-bit socket-length
fields and glibc's size_t fields; compiling only GNU does not check both ABIs.
After building and packaging an executable target supported by the controller,
run the native smoke tests against that exact variant:

```bash
VSM_NATIVE_TEST_TARGET=x86_64-unknown-linux-musl uv run pytest tests/test_native.py::NativeTests tests/test_rup_native.py::RupCliCompatibilityTests
```

This selector affects tests only and does not enable foreign execution on the
controller. AArch64 runtime acceptance still requires a suitable device or emulator.

Host/SSH launches and process discovery require `targetctl` in the bundle.
It uses the same manifest and ABI checks; its control protocol has no revision
tags. See [targetctl](targetctl.md).
