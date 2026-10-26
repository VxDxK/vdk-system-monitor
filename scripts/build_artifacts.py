"""Build selected targets and publish inspected, content-addressed native artifacts."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vsm._elf import inspect_elf


def check_cross(parser: argparse.ArgumentParser, version: str) -> str:
    """Check the same container engine cross will use before starting any build."""
    try:
        # cross 0.2.5 also invokes Cargo for --version. Capture its irrelevant
        # missing-workspace warning when called from this checkout's root.
        output = subprocess.check_output(
            ["cross", "--version"], text=True, stderr=subprocess.PIPE, cwd=ROOT, timeout=15
        ).split()
    except (subprocess.SubprocessError, OSError) as error:
        parser.exit(1, f"cannot query cross version: {error}\n")
    if len(output) < 2 or output[:2] != ["cross", version]:
        parser.error(f"cross {version} is required")
    engine = os.environ.get("CROSS_CONTAINER_ENGINE")
    if engine is None:
        engine = next((name for name in ("docker", "podman") if shutil.which(name)), None)
    if not engine or not shutil.which(engine):
        parser.error(
            "cross requires Docker or Podman; install a container engine or set "
            "CROSS_CONTAINER_ENGINE; see docs/native-builds.md"
        )
    try:
        subprocess.check_output(
            [engine, "info"], text=True, stderr=subprocess.STDOUT, cwd=ROOT, timeout=15
        )
    except subprocess.CalledProcessError as error:
        parser.exit(
            1,
            f"cross container engine {engine!r} is unavailable:\n{error.output.strip()}\n"
            "Make the engine accessible from this session, or configure builder = 'cargo' "
            "with a local target toolchain. No builds started; artifacts are unchanged. "
            "See docs/native-builds.md.\n",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"cannot contact cross container engine {engine!r}: {error}\n")
    return engine


def build_command(target: str, config: dict, tools: list[str]) -> list[str]:
    return [
        config["builder"],
        "build",
        "--manifest-path",
        str(ROOT / "binary-utils/Cargo.toml"),
        "--release",
        "--locked",
        "--target",
        target,
        *[argument for tool in tools for argument in ("--bin", tool)],
    ]


def build_directory(target: str, builder: str, provenance: dict) -> Path:
    """Keep container-loaded host macros separate from controller-built macros."""
    base = ROOT / "binary-utils/target"
    if builder == "cargo":
        return base
    # --target separates target executables, but proc macros and build scripts
    # still live in <target-dir>/release. Never share these with local Cargo or
    # a different container image/toolchain, even for the same Rust target.
    identity = {
        "rustc": provenance["rustc"],
        "cross_version": provenance["cross_version"],
        "cross_config_sha256": provenance["cross_config_sha256"],
        "image_overrides": {
            name: value
            for name, value in os.environ.items()
            if name.startswith("CROSS_") and "IMAGE" in name
        },
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return base / "cross" / target / key


def describe(source: Path, target: str, config: dict, version: str, provenance: dict) -> dict:
    image = source.read_bytes()
    info = inspect_elf(image)
    architecture = target.split("-")[0]
    abi = "musl" if target.endswith("-musl") else "glibc"
    if info.architecture != architecture or info.linkage != config["linkage"]:
        raise ValueError(f"{source}: configured target/ELF mismatch")
    if abi == "musl" and (info.interpreter or info.needed or info.minimum_glibc):
        raise ValueError(f"{source}: portable musl artifact is not fully static")
    if abi == "glibc" and (not info.interpreter or "libc.so.6" not in info.needed):
        raise ValueError(f"{source}: expected a GNU/glibc dynamic executable")
    digest = hashlib.sha256(image).hexdigest()
    return {
        **asdict(info),
        "needed": list(info.needed),
        "format": "elf",
        "file": f"{target}/{source.name}-{digest}.bin",
        "sha256": digest,
        "version": version,
        "target": target,
        "abi": abi,
        "kernel_family": "linux",
        "profiles": config["profiles"],
        "cpu_features": config.get("cpu_features", []),
        "minimum_kernel": None,
        "minimum_api": None,
        "provenance": provenance,
        "verification": "build-only",
    }


def load_index(output: Path) -> dict:
    path = output / "manifest.json"
    if not path.exists():
        return {"tools": {}}
    index = json.loads(path.read_text())
    if not isinstance(index, dict):
        raise TypeError("artifact manifest must be an object")
    if "version" in index:
        raise ValueError("obsolete artifact manifest; rebuild the bundle")
    if not isinstance(index.get("tools"), dict) or any(
        not isinstance(variants, list) or not variants for variants in index["tools"].values()
    ):
        raise ValueError("invalid artifact manifest tools")
    return index


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--target", action="append", help="configured Rust triple (repeatable)")
    selection.add_argument("--all", action="store_true", help="build all configured targets")
    parser.add_argument("--tool", action="append", help="tool name (repeatable)")
    parser.add_argument("--config", type=Path, default=ROOT / "scripts/build-targets.toml")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args(argv)
    config = tomllib.loads(args.config.read_text())
    targets = list(
        dict.fromkeys(list(config["targets"]) if args.all else args.target or [config["default"]])
    )
    selected_tools = {}
    supported = {
        "x86_64-unknown-linux-gnu",
        "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-musl",
    }
    for target in targets:
        if target not in config["targets"] or target not in supported:
            parser.error(f"unknown or unsupported target: {target}")
        entry = config["targets"][target]
        expected_linkage = "static" if target.endswith("musl") else "dynamic"
        if entry["linkage"] != expected_linkage:
            parser.error(f"invalid linkage for {target}")
        if not isinstance(entry["rustflags"], list) or not all(
            isinstance(flag, str) for flag in entry["rustflags"]
        ):
            parser.error(f"invalid rustflags for {target}")
        if entry["builder"] not in ("cargo", "cross"):
            parser.error(f"unsupported builder for {target}")
        if not entry["profiles"] or not set(entry["profiles"]) <= {
            "linux",
            "android",
            "openharmony",
        }:
            parser.error(f"invalid execution profiles for {target}")
        available = entry.get("tools", config["tools"])
        tools = list(dict.fromkeys(args.tool or available))
        if not tools:
            parser.error(f"no tools selected for {target}")
        for tool in tools:
            if (
                not isinstance(tool, str)
                or re.fullmatch(r"[A-Za-z0-9_-]+", tool) is None
                or tool not in available
                or tool not in config["tools"]
                or not (ROOT / f"binary-utils/src/bin/{tool}.rs").is_file()
            ):
                parser.error(f"unknown or unavailable tool for {target}: {tool}")
        features = entry.get("cpu_features", [])
        if not isinstance(features, list) or any(not isinstance(value, str) for value in features):
            parser.error(f"invalid cpu_features for {target}")
        selected_tools[target] = tools
    # Validate the entire selection and prerequisites before any build starts.
    container_engine = None
    for builder in sorted({config["targets"][target]["builder"] for target in targets}):
        if not shutil.which(builder):
            parser.error(f"{builder} unavailable; see docs/native-builds.md")
        if builder == "cross":
            container_engine = check_cross(parser, config["cross_version"])
    metadata = json.loads(
        subprocess.check_output(
            [
                "cargo",
                "metadata",
                "--manifest-path",
                str(ROOT / "binary-utils/Cargo.toml"),
                "--format-version",
                "1",
                "--no-deps",
                "--locked",
            ],
            text=True,
            cwd=ROOT,
        )
    )
    package = next(item for item in metadata["packages"] if item["name"] == "binary-utils")
    provenance = {
        "rustc": subprocess.check_output(["rustc", "-vV"], text=True, cwd=ROOT).strip(),
        "source_sha256": hashlib.sha256(
            b"".join(
                str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes()
                for path in sorted((ROOT / "binary-utils/src").rglob("*.rs"))
            )
        ).hexdigest(),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "cross_version": config["cross_version"],
        "cross_config_sha256": hashlib.sha256((ROOT / "Cross.toml").read_bytes()).hexdigest()
        if (ROOT / "Cross.toml").exists()
        else None,
        "cargo_lock_sha256": hashlib.sha256(
            (ROOT / "binary-utils/Cargo.lock").read_bytes()
        ).hexdigest(),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    # A single writer protects subset rebuilds from losing concurrent index updates.
    # Build controllers currently require Unix, like the existing development setup.
    import fcntl

    with (args.output / ".build.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = load_index(args.output)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=args.output) as staging:
            staged = Path(staging)
            for target in targets:
                entry = config["targets"][target]
                tools = selected_tools[target]
                target_directory = build_directory(target, entry["builder"], provenance)
                environment = os.environ.copy()
                environment["CARGO_TARGET_DIR"] = str(target_directory)
                environment["CROSS_CONFIG"] = str(ROOT / "Cross.toml")
                if entry["builder"] == "cross":
                    environment["CROSS_CONTAINER_ENGINE"] = container_engine
                environment.pop("RUSTFLAGS", None)
                baseline = "x86-64" if target.startswith("x86_64") else "generic"
                flags = ["-C", f"target-cpu={baseline}", *entry["rustflags"]]
                environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
                try:
                    subprocess.run(
                        build_command(target, entry, tools), cwd=ROOT, env=environment, check=True
                    )
                except subprocess.CalledProcessError as error:
                    parser.exit(
                        1,
                        f"{entry['builder']} build failed for {target} (exit {error.returncode}); "
                        "artifact manifest unchanged. See compiler/container diagnostics above.\n",
                    )
                except OSError as error:
                    parser.exit(1, f"cannot start {entry['builder']} for {target}: {error}\n")
                for tool in tools:
                    source = target_directory / target / "release" / tool
                    item = describe(
                        source,
                        target,
                        entry,
                        package["version"],
                        {
                            **provenance,
                            "builder": entry["builder"],
                            "cross_version": config["cross_version"]
                            if entry["builder"] == "cross"
                            else None,
                            "rustflags": flags,
                        },
                    )
                    destination = staged / item["file"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    if hashlib.sha256(destination.read_bytes()).hexdigest() != item["sha256"]:
                        raise ValueError(f"{source}: executable changed during packaging")
                    destination.chmod(0o755)
                    variants = index["tools"].setdefault(tool, [])
                    variants[:] = [old for old in variants if old["target"] != target]
                    variants.append(item)
            # Content-addressed paths keep the previous manifest valid even if publication stops.
            for source in staged.rglob("*.bin"):
                destination = args.output / source.relative_to(staged)
                if not destination.resolve().is_relative_to(args.output.resolve()):
                    raise ValueError("publication path escapes the artifact bundle")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
            manifest = staged / "manifest.json"
            manifest.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
            os.replace(manifest, args.output / "manifest.json")
    print(args.output / "manifest.json")


if __name__ == "__main__":
    main()
