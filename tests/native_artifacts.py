"""Locate the selected target for controller-native executable tests."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def native_artifact(name: str) -> Path:
    directory = ROOT / "artifacts"
    manifest = json.loads((directory / "manifest.json").read_text())
    target = os.environ.get("VSM_NATIVE_TEST_TARGET", "x86_64-unknown-linux-gnu")
    item = next(variant for variant in manifest["tools"][name] if variant["target"] == target)
    return directory / item["file"]
