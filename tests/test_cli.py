"""Runtime contracts for source-derived capability documentation."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from vsm.cli._catalogue import entries
from vsm.cli._render import detail, parameters
from vsm.cli._source import parameter_docs, source_docs
from vsm.cli.main import main


#: Class documentation can be written as adjacent comments.
@dataclass
class Commented:
    #: This inherited parameter uses seconds.
    interval: float = 2.0
    tag: ClassVar[str] = "hidden"
    required: str = field(kw_only=True)  # A required identity, not a default.


@dataclass
class WithFactory(Commented):
    """An authored class docstring takes priority over runtime-generated docs."""

    value: str = "# this is a string"  # This is a real parameter comment.
    items: list[int] = field(default_factory=lambda: (_ for _ in ()).throw(AssertionError()))

    def __post_init__(self):
        raise AssertionError("documentation must never construct capabilities")


class CliTests(unittest.TestCase):
    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                status = main(list(arguments))
            except SystemExit as error:
                status = error.code
        return status, output.getvalue(), errors.getvalue()

    def test_root_and_docs_help_version_and_bare_command(self):
        for arguments in ((), ("--help",), ("docs", "--help")):
            with self.subTest(arguments=arguments):
                status, output, errors = self.invoke(*arguments)
                self.assertEqual(status, 0)
                self.assertIn("docs" if arguments != ("docs", "--help") else "--search", output)
                self.assertEqual(errors, "")
        status, output, errors = self.invoke("--version")
        self.assertEqual(status, 0)
        self.assertTrue(output.startswith("vsm "))
        self.assertEqual(errors, "")

    def test_top_level_help_does_not_import_capabilities(self):
        for argument in ("--help", "--version"):
            script = f"""import sys
from vsm.cli.main import main
try:
    main([{argument!r}])
except SystemExit as error:
    assert error.code == 0
for module in ("vsm.action.registry", "vsm.processor.registry", "pandas", "asyncssh", "plotly", "matplotlib"):
    assert module not in sys.modules, module
"""
            result = subprocess.run(
                [sys.executable, "-c", script], check=False, capture_output=True, timeout=15
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_discovery_matches_registries(self):
        from vsm.action.registry import ACTION_TYPES
        from vsm.connection.registry import CONNECTION_TYPES
        from vsm.processor.registry import PROCESSORS

        for category, registry in (
            ("actions", ACTION_TYPES),
            ("processors", PROCESSORS),
            ("connections", CONNECTION_TYPES),
        ):
            self.assertEqual({entry.name for entry in entries(category)}, set(registry))
        native_names = {
            path.stem
            for path in (Path(__file__).resolve().parents[1] / "binary-utils/src/bin").glob("*.rs")
        }
        self.assertEqual({entry.name for entry in entries("binaries")}, native_names)
        self.assertEqual(len(ACTION_TYPES), 50)
        self.assertEqual(len(PROCESSORS), 32)

    def test_all_builtins_have_source_descriptions_and_examples(self):
        for category in ("actions", "processors", "connections", "binaries"):
            for entry in entries(category):
                with self.subTest(category=category, name=entry.name):
                    self.assertNotIn("No description in source", entry.documentation)
                    self.assertIn("Example", detail(entry))
                    if entry.implementation:
                        self.assertIn("Import:", detail(entry))
                        self.assertFalse(
                            entry.documentation.startswith(entry.implementation.__name__ + "(")
                        )

    def test_list_brief_default_all_and_sorted_order(self):
        for category in ("actions", "processors", "connections", "binaries"):
            status, output, errors = self.invoke("docs", category, "--brief")
            self.assertEqual(status, 0)
            self.assertEqual(errors, "")
            self.assertEqual(
                output.splitlines()[1:-1], sorted(entry.name for entry in entries(category))
            )
            status, output, _ = self.invoke("docs", category)
            for entry in entries(category):
                self.assertIn(f"  {entry.name}: {entry.summary}", output)
        self.assertEqual(self.invoke("docs"), self.invoke("docs", "all"))
        for category in ("actions", "processors", "connections", "binaries"):
            self.assertIn(category + ":", self.invoke("docs")[1])

    def test_named_detail_inherited_parameters_and_lifecycle(self):
        status, output, errors = self.invoke("docs", "actions", "cpu_freq")
        self.assertEqual((status, errors), (0, ""))
        for text in (
            "CpuFreq",
            "interval: int (default: 250)",
            "duration",
            "seconds",
            "device.use",
            "scaling_cur_freq",
        ):
            self.assertIn(text, output)
        self.assertNotIn("schema_name:", output)
        self.assertIn("device.do", self.invoke("docs", "actions", "manual")[1])
        self.assertIn(
            "processes: ProcessSet (required)", self.invoke("docs", "actions", "smaps")[1]
        )
        self.assertIn("allow_partial", self.invoke("docs", "processors", "rup")[1])
        self.assertIn("destination: str (required)", self.invoke("docs", "connections", "ssh")[1])
        fio = self.invoke("docs", "actions", "fio")[1]
        self.assertIn("args: tuple[str, ...]", fio)
        self.assertNotIn("mode:", fio)
        self.assertIn("do() waits for completion", fio)
        self.assertIn("await capture.wait()", fio)

    def test_search_is_literal_case_insensitive_or_and_covers_details(self):
        status, output, errors = self.invoke("docs", "actions", "-s", "SCALING_CUR_FREQ", "--brief")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("cpu_freq", output)
        status, output, _ = self.invoke("docs", "actions", "-s", "[", "--brief")
        self.assertEqual(status, 0)
        self.assertIn("cpu_freq", output)
        status, output, _ = self.invoke(
            "docs", "connections", "-s", "HOST", "-s", "SFTP", "--brief"
        )
        self.assertEqual(status, 0)
        self.assertIn("host", output)
        self.assertIn("ssh", output)
        status, output, _ = self.invoke("docs", "-s", "no-such-capability-98765")
        self.assertEqual(status, 0)
        self.assertEqual(output.count("No matching capabilities."), 4)

    def test_usage_errors(self):
        for arguments in (
            ("unknown",),
            ("docs", "conversions"),
            ("docs", "actions", "unknown"),
            ("docs", "all", "cpu_freq"),
            ("docs", "actions", "cpu_freq", "--brief"),
            ("docs", "actions", "cpu_freq", "--search", "cpu"),
            ("docs", "actions", "--search"),
            ("docs", "--unknown"),
        ):
            with self.subTest(arguments=arguments):
                status, output, errors = self.invoke(*arguments)
                self.assertEqual(status, 2)
                self.assertEqual(output, "")
                self.assertIn("error:", errors)

    def test_no_target_or_native_execution_or_artifacts_required(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("documentation attempted target or native access")

        with (
            patch("vsm.connection.host.Host.connect", forbidden),
            patch("vsm.connection.ssh.SSH.connect", forbidden),
            patch("vsm.binary.BinaryProvider.resolve", forbidden),
            patch("subprocess.run", forbidden),
            patch("subprocess.check_output", forbidden),
        ):
            status, output, errors = self.invoke("docs", "all", "-s", "")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("rfpoll", output)

    def test_failures_and_interruptions(self):
        for error, expected in (
            (OSError("unreadable source"), 1),
            (RuntimeError("missing sources"), 1),
            (ValueError("invalid source"), 1),
            (KeyboardInterrupt(), 130),
        ):
            with patch("vsm.cli.docs.run", side_effect=error):
                status, output, errors = self.invoke("docs")
            self.assertEqual(status, expected)
            self.assertEqual(output, "")
            self.assertIn("vsm:", errors)

    def test_python_source_comments_docstrings_and_safe_defaults(self):
        self.assertIn("adjacent comments", source_docs(Commented).description)
        self.assertIn("authored class docstring", source_docs(WithFactory).description)
        docs = parameter_docs(WithFactory)
        self.assertIn("seconds", docs["interval"])
        self.assertIn("real parameter comment", docs["value"])
        self.assertNotIn("this is a string", docs["value"])
        rendered = "\n".join(parameters(WithFactory))
        self.assertIn("required: str (required)", rendered)
        self.assertIn("default factory (not evaluated)", rendered)
        self.assertNotIn("tag:", rendered)

    def test_module_execution(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, "-m", "vsm", "docs", "connections", "--brief"],
            check=False,
            capture_output=True,
            timeout=15,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"ssh", result.stdout)

    def test_invalid_catalogue_category(self):
        with self.assertRaisesRegex(ValueError, "unknown documentation category"):
            entries("invalid")

    def test_native_sources_missing_is_an_operational_error(self):
        with (
            patch("vsm.cli._native.files", return_value=Path("/missing-vsm-resources")),
            patch("vsm.cli._native.__file__", "/missing-vsm-checkout/a/b/c/source.py"),
        ):
            status, _, errors = self.invoke("docs", "binaries")
        self.assertEqual(status, 1)
        self.assertIn("native documentation sources are missing", errors)
