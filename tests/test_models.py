"""Public value-object ergonomics, immutability, and ownership preservation."""

import os
import unittest
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from pathlib import PurePosixPath

from vsm.models import ActionId, DeviceId, ProcessIdentity, ProcessSet, TargetPath


class ModelTests(unittest.TestCase):
    def process(self, pid):
        return ProcessIdentity(
            device_id=DeviceId("host:test"),
            pid=pid,
            start_ticks=99,
            boot_id="boot",
            name="worker",
        )

    def test_process_selection_sequence_and_slices(self):
        first, second = self.process(10), self.process(20)
        selection = ProcessSet((first, second, first))
        self.assertIsInstance(selection, Sequence)
        self.assertEqual(len(selection), 3)
        self.assertTrue(selection)
        self.assertEqual(list(selection), [first, second, first])
        self.assertEqual(list(reversed(selection)), [first, second, first])
        self.assertIs(selection[0], first)
        self.assertIs(selection[-2], second)
        self.assertIn(second, selection)
        self.assertNotIn(self.process(30), selection)
        self.assertEqual(selection.count(first), 2)
        self.assertEqual(selection.index(second), 1)
        for index in (slice(1, 3), slice(None, None, -1), slice(None, None, 2), slice(10)):
            with self.subTest(index=index):
                subset = selection[index]
                self.assertIsInstance(subset, ProcessSet)
                self.assertEqual(tuple(subset), tuple(selection)[index])
        self.assertEqual(selection[1:1], ProcessSet())
        for index in (3, -4):
            with self.assertRaises(IndexError):
                selection[index]
        with self.assertRaises(ValueError):
            selection[::0]

    def test_process_selection_snapshots_input_and_is_hashable(self):
        first = self.process(10)
        items = [first]
        selection = ProcessSet(items)
        items.clear()
        self.assertEqual(tuple(selection), (first,))
        self.assertEqual(selection, ProcessSet(p for p in (first,)))
        self.assertEqual(hash(selection), hash(ProcessSet((first,))))
        self.assertNotEqual(selection, (first,))
        self.assertFalse(ProcessSet())
        self.assertEqual(len(ProcessSet()), 0)
        with self.assertRaises(FrozenInstanceError):
            selection._items = ()
        with self.assertRaises(TypeError):
            selection[0] = first
        self.assertFalse(hasattr(selection, "processes"))
        with self.assertRaises(TypeError):
            ProcessSet(processes=(first,))
        with self.assertRaises(TypeError):
            ProcessSet(items=(first,))

    def test_identity_formatting_retains_type_and_validation(self):
        for cls in (DeviceId, ActionId):
            identity = cls("phone serial")
            self.assertEqual(str(identity), "phone serial")
            self.assertEqual(f"id={identity}", "id=phone serial")
            self.assertEqual(identity.value, "phone serial")
            self.assertEqual(identity, cls("phone serial"))
            self.assertEqual(hash(identity), hash(cls("phone serial")))
            self.assertNotEqual(identity, "phone serial")
            for invalid in ("", "\0", 12):
                with self.assertRaises(ValueError):
                    cls(invalid)
        self.assertNotEqual(DeviceId("same"), ActionId("same"))

    def test_target_path_operations_preserve_device(self):
        device = DeviceId("ssh:server")
        path = TargetPath(device, PurePosixPath("/tmp/work"))
        self.assertEqual(str(path), "/tmp/work")
        self.assertEqual(path.name, "work")
        self.assertEqual(path.parent, TargetPath(device, PurePosixPath("/tmp")))
        for operand in ("child", PurePosixPath("child"), "/other", "", "../sibling"):
            with self.subTest(operand=operand):
                result = path / operand
                self.assertIs(result.device_id, device)
                self.assertEqual(result.value, path.value / operand)
        root = TargetPath(device, PurePosixPath("/"))
        self.assertEqual(root.parent, root)
        self.assertEqual(root.name, "")
        for invalid in (path, TargetPath(DeviceId("other"), path.value), 123):
            with self.assertRaises(TypeError):
                path / invalid
        with self.assertRaises(TypeError):
            os.fspath(path)
