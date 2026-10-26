"""Artifact compatibility and deployment contracts, without foreign execution."""

import asyncio
import hashlib
import json
import struct
import tempfile
import unittest
from contextlib import chdir
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from support import FakeConnection

from vsm._elf import inspect_elf
from vsm.binary import (
    BinaryPreference,
    BinaryProvider,
    TargetProfile,
    default_artifact_directory,
)
from vsm.models import CapabilityUnavailable, Completed, TargetInfo
from vsm.runtime import Run


def elf(architecture="aarch64", dynamic=False):
    image = bytearray(1024)
    image[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into(
        "<HH", image, 16, 3 if dynamic else 2, 183 if architecture == "aarch64" else 62
    )
    struct.pack_into("<Q", image, 24, 768)
    struct.pack_into("<Q", image, 32, 64)
    struct.pack_into("<HH", image, 54, 56, 3 if dynamic else 1)
    struct.pack_into("<IIQQQQQQ", image, 64, 1, 5, 0, 0, 0, 1024, 1024, 4096)
    if dynamic:
        interpreter = b"/lib64/ld-linux-x86-64.so.2\0"
        image[256 : 256 + len(interpreter)] = interpreter
        struct.pack_into(
            "<IIQQQQQQ", image, 120, 3, 4, 256, 256, 0, len(interpreter), len(interpreter), 1
        )
        struct.pack_into("<IIQQQQQQ", image, 176, 2, 4, 320, 320, 0, 80, 80, 8)
        strings = b"\0libc.so.6\0libgcc_s.so.1\0GLIBC_2.17\0"
        image[512 : 512 + len(strings)] = strings
        for index, entry in enumerate(((5, 512), (10, len(strings)), (1, 1), (1, 11), (0, 0))):
            struct.pack_into("<qQ", image, 320 + index * 16, *entry)
    return bytes(image)


def variant(root, architecture="aarch64", dynamic=False, suffix=""):
    image = elf(architecture, dynamic)
    info = inspect_elf(image)
    target = f"{architecture}-unknown-linux-{'gnu' if dynamic else 'musl'}"
    location = f"{target}/rfpoll{suffix}.bin"
    path = root / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    return {
        **asdict(info),
        "needed": list(info.needed),
        "format": "elf",
        "target": target,
        "file": location,
        "sha256": hashlib.sha256(image).hexdigest(),
        "version": "fixture",
        "kernel_family": "linux",
        "profiles": ["linux", "android", "openharmony"],
        "abi": "glibc" if dynamic else "musl",
        "cpu_features": [],
        "minimum_kernel": None,
        "minimum_api": None,
    }


def write_manifest(root, variants):
    (root / "manifest.json").write_text(json.dumps({"tools": {"rfpoll": variants}}))


class BinaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.connection = FakeConnection()
        self.connection._target = TargetInfo(
            os="android",
            kernel_family="linux",
            architecture="arm64",
            kernel="5.10.0",
            abi="bionic",
        )
        self.item = variant(self.root)
        write_manifest(self.root, [self.item])

    async def test_static_musl_on_bionic_and_unknown_libc(self):
        provider = BinaryProvider(self.root)
        first = await provider.resolve(self.connection, "rfpoll")
        self.connection._target = replace(self.connection.target, os="openharmony", abi=None)
        self.assertEqual(await provider.resolve(self.connection, "rfpoll"), first)
        self.assertEqual(len(self.connection.commands), 2)  # chmod only; no target execution.

    async def test_deployment_reuse_workspace_isolation_and_corruption(self):
        provider = BinaryProvider(self.root)
        paths = await asyncio.gather(
            *(provider.resolve(self.connection, "rfpoll") for _ in range(4))
        )
        self.assertEqual(len(set(paths)), 1)
        other = FakeConnection()
        other._target = self.connection.target
        other._tmp_dir = other.path("/tmp/other-workspace")
        self.assertNotEqual(await provider.resolve(other, "rfpoll"), paths[0])
        self.connection.files[str(paths[0])] = b"corrupted"
        with self.assertRaisesRegex(CapabilityUnavailable, "deployed binary hash mismatch"):
            await provider.resolve(self.connection, "rfpoll")

    async def test_profile_supplies_unknown_kernel_and_rejects_conflicts(self):
        self.connection._target = replace(self.connection.target, kernel_family=None)
        with self.assertRaisesRegex(CapabilityUnavailable, "kernel family"):
            await BinaryProvider(self.root).resolve(self.connection, "rfpoll")
        await BinaryProvider(
            self.root, target_profile=TargetProfile(kernel_family="linux")
        ).resolve(self.connection, "rfpoll")
        with self.assertRaisesRegex(CapabilityUnavailable, "conflicts"):
            await BinaryProvider(self.root, target_profile=TargetProfile(abi="musl")).resolve(
                self.connection, "rfpoll"
            )

    async def test_static_preferred_and_explicit_preference_validated(self):
        static = variant(self.root, "x86_64")
        dynamic = variant(self.root, "x86_64", dynamic=True)
        write_manifest(self.root, [dynamic, static])
        self.connection._target = TargetInfo(
            os="linux", architecture="amd64", kernel="6.1", abi="glibc", abi_version="2.40"
        )
        selected = await BinaryProvider(self.root).resolve(self.connection, "rfpoll")
        self.assertEqual(selected.name, static["sha256"] + ".bin")
        selected = await BinaryProvider(
            self.root, preferences=(BinaryPreference("rfpoll", dynamic["target"]),)
        ).resolve(self.connection, "rfpoll")
        self.assertEqual(selected.name, dynamic["sha256"] + ".bin")
        self.assertIn("--list", self.connection.commands[-1].argv)
        self.connection._target = replace(self.connection.target, abi="bionic")
        with self.assertRaisesRegex(CapabilityUnavailable, "ABI"):
            await BinaryProvider(
                self.root, preferences=(BinaryPreference("rfpoll", dynamic["target"]),)
            ).resolve(self.connection, "rfpoll")

    async def test_ambiguity_and_duplicate_preferences(self):
        write_manifest(self.root, [self.item, variant(self.root, suffix="-second")])
        with self.assertRaisesRegex(CapabilityUnavailable, "ambiguous"):
            await BinaryProvider(self.root).resolve(self.connection, "rfpoll")
        preference = BinaryPreference("rfpoll", self.item["target"])
        with self.assertRaisesRegex(CapabilityUnavailable, "multiple explicit"):
            await BinaryProvider(self.root, preferences=(preference, preference)).resolve(
                self.connection, "rfpoll"
            )

    async def test_incompatible_unknown_and_corrupt_requirements(self):
        cases = (
            ({"architecture": "x86_64"}, "architecture"),
            ({"bits": 32}, "bits"),
            ({"endianness": "big"}, "endianness"),
            ({"format": "pe"}, "format"),
            ({"cpu_features": ["sve"]}, "CPU features"),
            ({"minimum_kernel": "6.0"}, "kernel version"),
            ({"minimum_api": 30}, "API level"),
            ({"target": "aarch64-linux-android"}, "target triple"),
            ({"sha256": "0" * 64}, "hash mismatch"),
            ({"file": "../outside.bin"}, "inside the bundle"),
            ({"file": "/tmp/outside.bin"}, "inside the bundle"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                write_manifest(self.root, [{**self.item, **changes}])
                with self.assertRaisesRegex(CapabilityUnavailable, message):
                    await BinaryProvider(self.root).resolve(self.connection, "rfpoll")
        write_manifest(self.root, [self.item])
        (self.root / self.item["file"]).write_bytes(b"damaged")
        with self.assertRaisesRegex(CapabilityUnavailable, "hash mismatch"):
            await BinaryProvider(self.root).resolve(self.connection, "rfpoll")

    async def test_symlink_directory_escape(self):
        with tempfile.TemporaryDirectory() as external:
            (Path(external) / "tool.bin").write_bytes(elf())
            (self.root / "outside").symlink_to(external, target_is_directory=True)
            write_manifest(self.root, [{**self.item, "file": "outside/tool.bin"}])
            with self.assertRaisesRegex(CapabilityUnavailable, "escapes"):
                await BinaryProvider(self.root).resolve(self.connection, "rfpoll")

    async def test_version_errors_and_missing_tool(self):
        for version in (None, True, 3):
            (self.root / "manifest.json").write_text(json.dumps({"version": version}))
            with self.assertRaisesRegex(CapabilityUnavailable, "obsolete artifact manifest"):
                await BinaryProvider(self.root).resolve(self.connection, "rfpoll")
        write_manifest(self.root, [self.item])
        with self.assertRaisesRegex(CapabilityUnavailable, "unknown binary"):
            await BinaryProvider(self.root).resolve(self.connection, "missing")

    async def test_dynamic_versions_libraries_and_loader_failure(self):
        item = variant(self.root, "x86_64", dynamic=True)
        write_manifest(self.root, [item])
        target = TargetInfo(os="linux", architecture="x86_64", kernel="6.1", abi="glibc")
        for updates, message in (
            ({}, "libc version"),
            ({"abi_version": "2.16"}, "libc version"),
            ({"abi_version": "2.40", "available_libraries": ()}, "libraries"),
            ({"abi_version": "2.40", "interpreter": "/wrong"}, "interpreter"),
        ):
            self.connection._target = replace(target, **updates)
            with self.assertRaisesRegex(CapabilityUnavailable, message):
                await BinaryProvider(self.root).resolve(self.connection, "rfpoll")
        self.connection._target = replace(target, abi_version="2.40")
        original = self.connection.run

        async def run(command, **kwargs):
            if "--list" in command.argv:
                return Completed(exit_code=1, stderr=b"missing shared library")
            return await original(command, **kwargs)

        self.connection.run = run
        with self.assertRaisesRegex(CapabilityUnavailable, "missing shared library"):
            await BinaryProvider(self.root).resolve(self.connection, "rfpoll")

    async def test_dynamic_loader_uses_caller_deadline_and_new_connection_signature(self):
        item = variant(self.root, "x86_64", dynamic=True)
        write_manifest(self.root, [item])
        self.connection._target = TargetInfo(
            os="linux", architecture="x86_64", kernel="6.1", abi="glibc", abi_version="2.40"
        )
        original = self.connection.run
        blocked = False
        loader_calls = []

        async def run(command, *, check=True, stdout=None, stderr=None):
            if "--list" in command.argv:
                loader_calls.append(command)
                self.assertFalse(check)
                if blocked:
                    await asyncio.Event().wait()
                return Completed(exit_code=0)
            return await original(command, check=check, stdout=stdout, stderr=stderr)

        self.connection.run = run
        provider = BinaryProvider(self.root)
        await provider.resolve(self.connection, "rfpoll")
        self.assertEqual(len(loader_calls), 1)
        blocked = True
        real_timeout = asyncio.timeout
        with (
            patch(
                "vsm.binary.asyncio.timeout", side_effect=lambda seconds: real_timeout(0)
            ) as timer,
            self.assertRaisesRegex(CapabilityUnavailable, "dynamic loader check failed") as caught,
        ):
            await provider.resolve(self.connection, "rfpoll")
        timer.assert_called_once_with(10)
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)

    async def test_legacy_manifest(self):
        item = variant(self.root, "x86_64", dynamic=True)
        (self.root / "legacy.bin").write_bytes((self.root / item["file"]).read_bytes())
        (self.root / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "os": "linux",
                    "architecture": "x86_64",
                    "abi": "glibc",
                    "minimum_glibc": "2.17",
                    "tools": {
                        "rfpoll": {
                            "file": "legacy.bin",
                            "sha256": item["sha256"],
                        }
                    },
                }
            )
        )
        self.connection._target = TargetInfo(
            os="linux", architecture="amd64", kernel="6.1", abi="glibc", abi_version="2.40"
        )
        with self.assertRaisesRegex(CapabilityUnavailable, "obsolete artifact manifest"):
            await BinaryProvider(self.root).resolve(self.connection, "rfpoll")

    async def test_defaults_are_lazy_and_explicit_directory_wins(self):
        with patch("vsm.binary.default_artifact_directory", return_value=self.root):
            self.assertIsNone(BinaryProvider().directory)
            run = Run(self.root / "run")
            await run.binaries.resolve(self.connection, "rfpoll")
            async with run:
                pass
        with patch("vsm.binary.default_artifact_directory", side_effect=AssertionError("lookup")):
            await BinaryProvider(self.root).resolve(self.connection, "rfpoll")
            async with Run(self.root / "no-native"):
                pass

    async def test_missing_artifacts_and_installed_package(self):
        with (
            patch("vsm.binary.__file__", str(self.root / "site-packages/vsm/binary.py")),
            self.assertRaisesRegex(CapabilityUnavailable, "outside a source checkout"),
        ):
            await BinaryProvider().resolve(self.connection, "rfpoll")
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(CapabilityUnavailable, "just build-artifacts"),
        ):
            await BinaryProvider(Path(directory)).resolve(self.connection, "rfpoll")

    def test_default_independent_of_cwd(self):
        with chdir(self.root):
            self.assertEqual(
                default_artifact_directory(), Path(__file__).resolve().parents[1] / "artifacts"
            )

    def test_elf_truncated_and_wrong_machine(self):
        for image in (b"", elf()[:100], bytes(1024)):
            with self.assertRaises(ValueError):
                inspect_elf(image)
        image = bytearray(elf())
        struct.pack_into("<H", image, 18, 40)
        with self.assertRaises(ValueError):
            inspect_elf(bytes(image))
