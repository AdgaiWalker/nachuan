import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from nachuan.accounts import AccountPool, AccountState
from nachuan.health import HealthRunner, expires_soon


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotate_is_private_and_recovers_reauth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text(json.dumps({"apiKey": "old", "generation": 1}), encoding="utf-8")
            pool = AccountPool(directory)
            pool.load()
            pool._accounts["account"].state = AccountState.REAUTH_REQUIRED
            await pool.rotate("account", {"apiKey": "new"}, expected_generation=1, recovered=True)
            account = pool._accounts["account"]
            self.assertEqual(account.credential()["apiKey"], "new")
            self.assertEqual(account.state, AccountState.READY)
            self.assertEqual(account.credential_generation, 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_stale_refresh_does_not_overwrite_newer_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text(json.dumps({"apiKey": "new"}), encoding="utf-8")
            pool = AccountPool(directory)
            pool.load()
            with self.assertRaises(RuntimeError):
                await pool.rotate("account", {"apiKey": "stale"}, expected_generation=0)
            self.assertEqual(pool._accounts["account"].credential()["apiKey"], "new")

    async def test_expiry_requires_refresh_window(self):
        self.assertTrue(expires_soon({"expiresAt": time.time() + 60}, time.time()))
        self.assertFalse(expires_soon({"expiresAt": time.time() + 3600}, time.time()))
        self.assertFalse(expires_soon({}, time.time()))

    async def test_health_runner_does_not_overlap(self):
        runner = HealthRunner()
        running = False
        calls = 0
        async def check():
            nonlocal running, calls
            self.assertFalse(running)
            running = True
            calls += 1
            await asyncio.sleep(0.01)
            running = False
        self.assertTrue(await runner.run_once(check))
        self.assertEqual(calls, 1)
        await runner.close()


if __name__ == "__main__":
    unittest.main()
