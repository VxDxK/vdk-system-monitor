import tempfile
import unittest
from pathlib import Path

from collector_acceptance import command_deadline, finite_collection

from vsm.connection.host import Host


class HostCollectorAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_caller_deadline_reaps_command_and_connection_is_reusable(self):
        await command_deadline(self, Host())

    async def test_finite_collection_and_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            await finite_collection(self, Host(), Path(directory))
