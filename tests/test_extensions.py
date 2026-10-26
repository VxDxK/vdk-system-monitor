import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from vsm.action.launch import Launch
from vsm.binary import BinaryProvider
from vsm.connection.host import Host
from vsm.models import CapabilityUnavailable
from vsm.runtime import Registry, Run, Services
from vsm.util.network import Endpoint


class ExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_structural_action_and_service(self):
        @dataclass(frozen=True)
        class Service:
            value: int

        @dataclass(frozen=True)
        class CustomAction:
            async def run(self, ctx):
                return ctx.service(Service).value

        with tempfile.TemporaryDirectory() as d:
            async with Run(Path(d) / "run") as run:
                run.services.register(Service, Service(42))
                host = await run.connect(Host())
                self.assertEqual(await host.do(CustomAction()), 42)

    async def test_supplied_executable_launch(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "script"
            script.write_text('#!/bin/sh\nprintf "supplied"\n')
            script.chmod(0o755)
            async with Run(Path(d) / "run") as run:
                host = await run.connect(Host())
                async with host.use(Launch(executable=script, argv=())) as process:
                    self.assertEqual((await process.wait()).stdout, b"supplied")

    async def test_artifact_identity_and_corruption_rejected(self):
        import shutil

        source = Path(__file__).resolve().parents[1] / "artifacts"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(source, root / "artifacts")
            async with Host().connect() as conn:
                provider = BinaryProvider(root / "artifacts")
                path = await provider.resolve(conn, "rfpoll")
                self.assertTrue(Path(path.value).exists())
                Path(path.value).write_bytes(b"bad")
                with self.assertRaises(CapabilityUnavailable):
                    await provider.resolve(conn, "rfpoll")
                with self.assertRaises(CapabilityUnavailable):
                    await provider.resolve(conn, "unknown")

    def test_registry_and_typed_service_duplicates(self):
        registry = Registry()
        registry.register("extension", int)
        self.assertIs(registry.resolve("extension"), int)
        with self.assertRaises(ValueError):
            registry.register("extension", str)
        services = Services()
        services.register(int, 42)
        self.assertEqual(services.get(int), 42)
        with self.assertRaises(ValueError):
            services.register(int, 43)
        with self.assertRaises(CapabilityUnavailable):
            services.get(str)

    def test_endpoint_types(self):
        self.assertEqual(str(Endpoint.parse("[::1]:1234")), "[::1]:1234")
        with self.assertRaises(ValueError):
            Endpoint.parse("127.0.0.1:0")
