import json
from pathlib import Path
import tempfile
import unittest

from nachuan.ledger import Ledger, UsageRecord


def record(request_id="req-1", success=True, cost=None):
    return UsageRecord(request_id, 1, "plugin-a", "model-a", "account-a", False, success, 100, 10, 5, 15, "upstream", cost, "credit")


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_idempotent_request_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "usage.json")
            self.assertTrue(await ledger.record(record()))
            self.assertFalse(await ledger.record(record()))
            snapshot = ledger.snapshot()
            self.assertEqual(snapshot["totals"]["total_tokens"], 15)
            self.assertEqual(snapshot["totals"]["requests"], 1)

    async def test_failure_does_not_count_success(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "usage.json")
            await ledger.record(record(success=False))
            self.assertEqual(ledger.snapshot()["totals"]["success_rate"], 0)

    async def test_vendor_cost_requires_upstream_report(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "usage.json")
            await ledger.record(record())
            self.assertNotIn("vendor_cost", ledger.snapshot()["totals"])
            await ledger.record(record("req-2", cost=1.5))
            self.assertEqual(ledger.snapshot()["totals"]["vendor_cost"], 1.5)

    async def test_persistence_and_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            ledger = Ledger(path)
            await ledger.record(record())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored = Ledger(path)
            self.assertEqual(restored.snapshot()["totals"]["total_tokens"], 15)
            self.assertNotIn("apiKey", path.read_text())


if __name__ == "__main__":
    unittest.main()
