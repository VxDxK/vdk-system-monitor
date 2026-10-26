"""Manifest validation and connection-owned executable deployment."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, fields, replace
from pathlib import Path, PurePosixPath
from weakref import WeakValueDictionary

from ._elf import inspect_elf, normalize_architecture
from .connection.connection import Connection
from .models import CapabilityUnavailable, DeviceId, Exec, TargetInfo, TargetPath

_locks: WeakValueDictionary[tuple[DeviceId, PurePosixPath, str], asyncio.Lock] = (
    WeakValueDictionary()
)


async def deploy(connection: Connection, source: Path, *, sha256: str | None = None) -> TargetPath:
    """Copy a regular executable into this connection's private workspace."""
    if not source.is_file() or source.is_symlink():
        raise ValueError("executable must be a regular file")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if sha256 is not None and digest != sha256:
        raise CapabilityUnavailable("binary hash mismatch")
    key = (connection.device_id, connection.tmp_dir.value, digest)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        path = connection.tmp_dir / (digest + ".bin")
        try:
            image = await connection.read_bytes(path)
        except FileNotFoundError:
            await connection.push(source, path)
            image = await connection.read_bytes(path)
        if hashlib.sha256(image).hexdigest() != digest:
            raise CapabilityUnavailable("deployed binary hash mismatch")
        # Also retry permissions after a cancelled or interrupted deployment.
        await connection.run(Exec(argv=("chmod", "700", str(path))))
        return path


def default_artifact_directory() -> Path:
    """Find artifacts in the imported source checkout, independently of cwd."""
    root = Path(__file__).resolve().parents[2]
    if not (
        (root / "src/vsm/binary.py").is_file()
        and (root / "binary-utils/Cargo.toml").is_file()
        and (root / "pyproject.toml").is_file()
    ):
        raise CapabilityUnavailable(
            "vsm is outside a source checkout; configure BinaryProvider(Path('/path/to/artifacts'))"
        )
    return root / "artifacts"


@dataclass(frozen=True, kw_only=True)
class TargetProfile:
    """Supplement unknown target facts; detected facts cannot be overridden."""

    os: str | None = None
    architecture: str | None = None
    kernel_family: str | None = None
    abi: str | None = None
    abi_version: str | None = None
    cpu_features: tuple[str, ...] | None = None
    api_level: int | None = None
    available_libraries: tuple[str, ...] | None = None
    interpreter: str | None = None

    def apply(self, target: TargetInfo) -> TargetInfo:
        updates = {}
        for field in fields(self):
            supplied = getattr(self, field.name)
            detected = getattr(target, field.name)
            if field.name == "architecture":
                supplied = normalize_architecture(supplied) if supplied else None
                detected = normalize_architecture(detected) if detected else None
            if supplied is None:
                continue
            if detected is not None and detected != supplied:
                raise CapabilityUnavailable(f"target profile conflicts with detected {field.name}")
            updates[field.name] = supplied
        return replace(target, **updates)


@dataclass(frozen=True)
class BinaryPreference:
    tool: str
    target: str


def _version(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _compatible(item: dict, target: TargetInfo) -> str | None:
    family = target.kernel_family or ("linux" if target.os == "linux" else None)
    if family != item["kernel_family"]:
        return "kernel family unknown or incompatible"
    if target.os not in item["profiles"]:
        return f"platform {target.os!r} is not a declared execution profile"
    if normalize_architecture(target.architecture) != item["architecture"]:
        return "architecture mismatch"
    required = item.get("cpu_features", [])
    if required and (target.cpu_features is None or not set(required) <= set(target.cpu_features)):
        return "CPU features unknown or incompatible"
    minimum_kernel = item.get("minimum_kernel")
    if minimum_kernel:
        import re

        match = re.match(r"[0-9]+(?:\.[0-9]+)*", target.kernel)
        if match is None or _version(match[0]) < _version(minimum_kernel):
            return "kernel version unknown or too old"
    minimum_api = item.get("minimum_api")
    if minimum_api is not None and (target.api_level is None or target.api_level < minimum_api):
        return "API level unknown or too old"
    if item["linkage"] == "dynamic":
        if not item.get("abi") or target.abi != item["abi"]:
            return "ABI unknown or incompatible"
        minimum = item.get("minimum_glibc")
        if minimum and (not target.abi_version or _version(target.abi_version) < _version(minimum)):
            return "target libc version unknown or too old"
        if target.interpreter is not None and target.interpreter != item["interpreter"]:
            return "interpreter mismatch"
        if target.available_libraries is not None and not set(item["needed"]) <= set(
            target.available_libraries
        ):
            return "required shared libraries unavailable"
    return None


def _variants(manifest: dict, name: str) -> list[dict]:
    if not isinstance(manifest, dict):
        raise TypeError("manifest must be an object")
    if "version" in manifest:
        raise ValueError("obsolete artifact manifest; rebuild the bundle")
    tools = manifest["tools"]
    if not isinstance(tools, dict):
        raise TypeError("manifest tools must be an object")
    if name not in tools:
        raise CapabilityUnavailable(f"unknown binary: {name}")
    variants = tools[name]
    if not isinstance(variants, list) or not variants:
        raise ValueError("tool variants must be a nonempty list")
    if any(not isinstance(item, dict) or "legacy" in item for item in variants):
        raise ValueError("invalid tool variant")
    return variants


def _artifact(directory: Path, item: dict) -> tuple[Path, dict]:
    if not isinstance(item, dict):
        raise TypeError("artifact must be an object")
    filename = item["file"]
    if not isinstance(filename, str) or not filename or "\\" in filename:
        raise ValueError("invalid artifact file location")
    relative = PurePosixPath(filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("artifact path must stay inside the bundle")
    source = directory / filename
    if source.is_symlink() or not source.resolve().is_relative_to(directory.resolve()):
        raise ValueError("artifact symlink or path escapes the bundle")
    image = source.read_bytes()
    if hashlib.sha256(image).hexdigest() != item["sha256"]:
        raise ValueError("binary hash mismatch")
    info = inspect_elf(image)
    actual = {
        "architecture": info.architecture,
        "format": "elf",
        "bits": info.bits,
        "endianness": info.endianness,
        "linkage": info.linkage,
        "interpreter": info.interpreter,
        "needed": list(info.needed),
        "minimum_glibc": info.minimum_glibc,
    }
    for key, value in actual.items():
        if item[key] != value:
            raise ValueError(f"manifest/ELF {key} mismatch")
    for field in ("cpu_features", "profiles"):
        if not isinstance(item[field], list) or any(
            not isinstance(value, str) or not value for value in item[field]
        ):
            raise ValueError(f"invalid {field}")
    if not item["profiles"]:
        raise ValueError("execution profiles must not be empty")
    for field in ("minimum_kernel", "minimum_glibc"):
        value = item.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"invalid {field}")
            _version(value)
    minimum_api = item.get("minimum_api")
    if minimum_api is not None and (type(minimum_api) is not int or minimum_api < 1):
        raise ValueError("invalid minimum_api")
    if item["linkage"] == "dynamic" and (
        item["abi"] != "glibc"
        or "libc.so.6" not in info.needed
        or not info.interpreter
        or not info.interpreter.startswith("/")
    ):
        raise ValueError("only GNU/glibc dynamic artifacts are currently supported")
    if item["linkage"] == "static" and (item["abi"] != "musl" or info.minimum_glibc is not None):
        raise ValueError("only musl static artifacts are currently supported")
    expected = f"{info.architecture}-unknown-linux-{'musl' if info.linkage == 'static' else 'gnu'}"
    if item["target"] != expected or item["kernel_family"] != "linux":
        raise ValueError("target triple disagrees with executable requirements")
    return source, item


@dataclass(frozen=True)
class BinaryProvider:
    """Select compatible native tools; default to this checkout's artifacts."""

    directory: Path | None = None
    preferences: tuple[BinaryPreference, ...] = ()
    target_profile: TargetProfile | None = None

    async def resolve(self, connection: Connection, name: str) -> TargetPath:
        directory = self.directory if self.directory is not None else default_artifact_directory()
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
        except FileNotFoundError:
            raise CapabilityUnavailable(
                f"artifact manifest missing at {directory / 'manifest.json'}; "
                "run just build-artifacts or configure BinaryProvider(Path('/path/to/artifacts'))"
            ) from None
        except (OSError, ValueError) as error:
            raise CapabilityUnavailable(f"cannot read artifact manifest: {error}") from error
        target = (
            self.target_profile.apply(connection.target)
            if self.target_profile
            else connection.target
        )
        rejected = []
        candidates = []
        preferences = [entry.target for entry in self.preferences if entry.tool == name]
        if len(preferences) > 1:
            raise CapabilityUnavailable(f"multiple explicit preferences for {name}")
        try:
            for index, item in enumerate(_variants(manifest, name)):
                label = (
                    item.get("target", f"variant {index}") if isinstance(item, dict) else str(index)
                )
                try:
                    source, item = _artifact(directory, item)
                    reason = _compatible(item, target)
                    if preferences and item["target"] != preferences[0]:
                        reason = "excluded by explicit target preference"
                    if reason:
                        rejected.append(f"{label}: {reason}")
                    else:
                        rank = 0 if item["linkage"] == "static" and item["abi"] == "musl" else 1
                        candidates.append((rank, source, item))
                except (KeyError, TypeError, ValueError, OSError) as error:
                    rejected.append(f"{label}: {error}")
        except (KeyError, TypeError, ValueError) as error:
            raise CapabilityUnavailable(f"invalid artifact manifest: {error}") from error
        if not candidates:
            raise CapabilityUnavailable(f"no compatible binary for {name}; " + "; ".join(rejected))
        best = min(entry[0] for entry in candidates)
        selected = [entry for entry in candidates if entry[0] == best]
        if len(selected) != 1:
            labels = ", ".join(entry[2]["target"] for entry in selected)
            raise CapabilityUnavailable(
                f"ambiguous binary for {name}: {labels}; set BinaryPreference"
            )
        _, source, item = selected[0]
        path = await deploy(connection, source, sha256=item["sha256"])
        if item["linkage"] == "dynamic":
            # Ask the actual target loader to resolve dependencies without running main().
            # No controller-local library assumptions or target Python are required.
            if not item["interpreter"]:
                raise CapabilityUnavailable("dynamic executable has no interpreter")
            try:
                async with asyncio.timeout(10):
                    result = await connection.run(
                        Exec(argv=(item["interpreter"], "--list", str(path))), check=False
                    )
            except (OSError, TimeoutError) as error:
                raise CapabilityUnavailable(
                    f"target dynamic loader check failed: {error}"
                ) from error
            if result.exit_code:
                raise CapabilityUnavailable(
                    f"target loader cannot resolve {name}: {result.stderr.decode(errors='replace')}"
                )
        return path
