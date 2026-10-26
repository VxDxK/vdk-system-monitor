"""Build orchestration and transactional subset publication using simulated builders."""

import argparse
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from test_binary import elf

SPEC = importlib.util.spec_from_file_location(
    "build_artifacts", Path(__file__).resolve().parents[1] / "scripts/build_artifacts.py"
)
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "artifacts"
        crate = self.root / "binary-utils"
        (crate / "src/bin").mkdir(parents=True)
        (crate / "Cargo.lock").write_text("fixture lock")
        (crate / "Cargo.toml").write_text("fixture manifest")
        for name in ("rfpoll", "pspider"):
            (crate / f"src/bin/{name}.rs").write_text("fn main() {}")
        self.config = self.root / "targets.toml"
        self.config.write_text("""
default = "x86_64-unknown-linux-gnu"
tools = ["rfpoll", "pspider"]
cross_version = "0.2.5"
[targets.x86_64-unknown-linux-gnu]
builder = "cargo"
linkage = "dynamic"
profiles = ["linux"]
rustflags = []
[targets.aarch64-unknown-linux-musl]
builder = "cross"
linkage = "static"
profiles = ["linux", "android"]
rustflags = ["-C", "target-feature=+crt-static"]
""")
        self.commands = []
        self.fail = False

    def output_for(self, command, **kwargs):
        if command[:2] == ["cargo", "metadata"]:
            return json.dumps({"packages": [{"name": "binary-utils", "version": "0.1.0"}]})
        if command[0] == "cross":
            return "cross 0.2.5"
        return "rustc fixture"

    def compile(self, command, **kwargs):
        self.commands.append((command, kwargs))
        target = command[command.index("--target") + 1]
        for index, value in enumerate(command):
            if value != "--bin":
                continue
            name = command[index + 1]
            path = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / target / "release" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            image = elf("x86_64", dynamic=True) if target.endswith("gnu") else elf()
            path.write_bytes(b"broken" if self.fail and name == "pspider" else image)

    def invoke(self, *arguments):
        with (
            patch.object(build, "ROOT", self.root),
            patch.object(build.shutil, "which", return_value="/fake/builder"),
            patch.object(build.subprocess, "check_output", side_effect=self.output_for),
            patch.object(build.subprocess, "run", side_effect=self.compile),
        ):
            build.main(["--config", str(self.config), "--output", str(self.output), *arguments])

    def test_all_targets_then_subset_preserves_other_variants(self):
        self.invoke("--all")
        index = json.loads((self.output / "manifest.json").read_text())
        self.assertNotIn("version", index)
        self.assertEqual(len(index["tools"]["rfpoll"]), 2)
        for variants in index["tools"].values():
            for item in variants:
                self.assertTrue((self.output / item["file"]).is_file())
                self.assertEqual(item["version"], "0.1.0")
        original = index["tools"]["pspider"]
        self.invoke("--target", "aarch64-unknown-linux-musl", "--tool", "rfpoll")
        index = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(index["tools"]["pspider"], original)
        self.assertEqual(len(index["tools"]["rfpoll"]), 2)
        command, options = self.commands[-1]
        self.assertEqual(command[0], "cross")
        self.assertEqual(command.count("--bin"), 1)
        self.assertIn("--locked", command)
        self.assertNotIn("RUSTFLAGS", options["env"])
        self.assertIn("+crt-static", options["env"]["CARGO_ENCODED_RUSTFLAGS"])

    def test_cross_never_reuses_host_proc_macros_or_stale_target_outputs(self):
        # Simulate the contaminated shared cache from a preceding host build.
        legacy = self.root / "binary-utils/target"
        (legacy / "release/deps").mkdir(parents=True)
        macro = legacy / "release/deps/libserde_derive.so"
        macro.write_bytes(b"requires the host's newer glibc")
        foreign = legacy / "aarch64-unknown-linux-musl/release/rfpoll"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"stale executable")
        self.invoke("--all")
        directories = {
            command[0]: Path(options["env"]["CARGO_TARGET_DIR"])
            for command, options in self.commands
        }
        self.assertEqual(directories["cargo"], legacy)
        self.assertNotEqual(directories["cross"], legacy)
        self.assertTrue(directories["cross"].is_relative_to(legacy / "cross"))
        self.assertFalse((directories["cross"] / "release/deps/libserde_derive.so").exists())
        self.assertEqual(macro.read_bytes(), b"requires the host's newer glibc")
        self.assertEqual(foreign.read_bytes(), b"stale executable")
        # Packaging used the fresh isolated output, not the stale shared path.
        index = json.loads((self.output / "manifest.json").read_text())
        item = next(
            entry for entry in index["tools"]["rfpoll"] if entry["target"].startswith("aarch64")
        )
        self.assertEqual((self.output / item["file"]).read_bytes(), elf())

    def test_cross_cache_is_stable_and_tracks_image_toolchain_and_target(self):
        identity = {
            "rustc": "rustc fixture",
            "cross_version": "0.2.5",
            "cross_config_sha256": "image A",
        }
        with patch.object(build, "ROOT", self.root):
            first = build.build_directory("x86_64-unknown-linux-musl", "cross", identity)
            self.assertEqual(
                first, build.build_directory("x86_64-unknown-linux-musl", "cross", identity)
            )
            for change in (
                {"rustc": "new rustc"},
                {"cross_config_sha256": "image B"},
                {"cross_version": "new cross"},
            ):
                self.assertNotEqual(
                    first,
                    build.build_directory(
                        "x86_64-unknown-linux-musl", "cross", {**identity, **change}
                    ),
                )
            self.assertNotEqual(
                first, build.build_directory("aarch64-unknown-linux-musl", "cross", identity)
            )
            with patch.dict(
                "os.environ", {"CROSS_TARGET_X86_64_UNKNOWN_LINUX_MUSL_IMAGE": "custom:image"}
            ):
                self.assertNotEqual(
                    first, build.build_directory("x86_64-unknown-linux-musl", "cross", identity)
                )

    def test_failed_validation_keeps_previous_index_and_files(self):
        self.invoke()
        manifest = self.output / "manifest.json"
        previous = manifest.read_bytes()
        self.fail = True
        with self.assertRaises(ValueError):
            self.invoke("--all")
        self.assertEqual(manifest.read_bytes(), previous)
        for variants in json.loads(previous)["tools"].values():
            for item in variants:
                self.assertTrue((self.output / item["file"]).is_file())

    def test_invalid_selection_fails_before_any_build(self):
        for arguments in (("--target", "arm-unknown"), ("--tool", "missing")):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.invoke(*arguments)
        self.assertEqual(self.commands, [])

    def test_obsolete_manifest_requires_fresh_bundle(self):
        self.output.mkdir()
        image = elf("x86_64", dynamic=True)
        (self.output / "legacy.bin").write_bytes(image)
        import hashlib

        (self.output / "manifest.json").write_text(
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
                            "sha256": hashlib.sha256(image).hexdigest(),
                            "version": "legacy fixture",
                        }
                    },
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "obsolete artifact manifest"):
            self.invoke("--target", "aarch64-unknown-linux-musl", "--tool", "rfpoll")

    def test_per_target_tool_selection(self):
        with self.config.open("a") as stream:
            stream.write('tools = ["rfpoll"]\n')
        self.invoke("--target", "aarch64-unknown-linux-musl")
        index = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(set(index["tools"]), {"rfpoll"})
        with self.assertRaises(SystemExit):
            self.invoke("--target", "aarch64-unknown-linux-musl", "--tool", "pspider")

    def test_container_access_failure_stops_all_before_building(self):
        original = self.output_for

        def unavailable(command, **kwargs):
            if command[-1] == "info":
                raise subprocess.CalledProcessError(
                    1, command, output="permission denied connecting to Docker API"
                )
            return original(command, **kwargs)

        errors = io.StringIO()
        with (
            patch.object(self, "output_for", side_effect=unavailable),
            patch.dict("os.environ", {"CROSS_CONTAINER_ENGINE": "docker"}),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            self.invoke("--all")
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.commands, [])
        self.assertFalse(self.output.exists())
        self.assertIn("No builds started", errors.getvalue())
        self.assertIn("permission denied", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_engine_override_is_honored_and_unavailable_engine_rejected(self):
        parser = argparse.ArgumentParser()
        with (
            patch.dict("os.environ", {"CROSS_CONTAINER_ENGINE": "podman"}),
            patch.object(build.shutil, "which", return_value="/fake/podman"),
            patch.object(
                build.subprocess, "check_output", side_effect=["cross 0.2.5", "engine info"]
            ) as query,
        ):
            self.assertEqual(build.check_cross(parser, "0.2.5"), "podman")
            self.assertEqual(query.call_args.args[0], ["podman", "info"])
        with (
            patch.dict("os.environ", {"CROSS_CONTAINER_ENGINE": "/missing/engine"}),
            patch.object(build.shutil, "which", return_value=None),
            patch.object(build.subprocess, "check_output", return_value="cross 0.2.5"),
            self.assertRaises(SystemExit),
        ):
            build.check_cross(parser, "0.2.5")

    def test_compiler_failure_reports_target_without_traceback(self):
        self.invoke()
        manifest = self.output / "manifest.json"
        original = manifest.read_bytes()
        errors = io.StringIO()
        with (
            patch.object(
                self, "compile", side_effect=subprocess.CalledProcessError(101, ["cargo", "build"])
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            self.invoke()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertIn("x86_64-unknown-linux-gnu", errors.getvalue())
        self.assertIn("exit 101", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_foreign_static_inspection_and_mislabeled_build_rejection(self):
        path = self.root / "rfpoll"
        path.write_bytes(elf())
        item = build.describe(
            path,
            "aarch64-unknown-linux-musl",
            {
                "linkage": "static",
                "profiles": ["linux"],
            },
            "0.1.0",
            {},
        )
        self.assertIsNone(item["interpreter"])
        self.assertEqual(item["architecture"], "aarch64")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            build.describe(
                path,
                "x86_64-unknown-linux-gnu",
                {
                    "linkage": "dynamic",
                    "profiles": ["linux"],
                },
                "0.1.0",
                {},
            )
