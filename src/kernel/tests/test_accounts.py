import asyncio
import json
from pathlib import Path
import random
import tempfile
import unittest

from nachuan.accounts import AccountPool, AccountState, Failure, FailureKind


class AccountPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pool = AccountPool(self.root, breaker_threshold=2, breaker_seconds=60, random_source=random.Random(0))
        for name in ("a", "b"):
            (self.root / f"{name}.json").write_text(json.dumps({"apiKey": f"secret-{name}"}), encoding="utf-8")
        self.pool.load()

    async def asyncTearDown(self):
        await self.pool.close()
        self.temp.cleanup()

    async def test_credential_is_not_exposed_in_snapshot(self):
        lease = await self.pool.acquire()
        self.assertEqual(lease.account.credential()["apiKey"], f"secret-{lease.account.id}")
        await lease.release(None)
        text = json.dumps(self.pool.snapshot())
        self.assertNotIn("secret-a", text)
        self.assertNotIn("apiKey", text)

    async def test_cooldown_expires_and_success_resets_failures(self):
        lease = await self.pool.acquire()
        uid = lease.account.id
        await lease.release(Failure(FailureKind.RATE_LIMIT, 0.01, "limited"))
        self.assertEqual(self.pool._accounts[uid].state, AccountState.COOLING)
        await asyncio.sleep(0.02)
        lease = await self.pool.acquire()
        self.assertNotEqual(lease.account.id, uid)
        await lease.release(None)

    async def test_transient_failures_open_circuit(self):
        lease = await self.pool.acquire()
        uid = lease.account.id
        await lease.release(Failure(FailureKind.TRANSIENT))
        lease = await self.pool.acquire()
        self.assertEqual(lease.account.id, uid)
        await lease.release(Failure(FailureKind.TRANSIENT))
        self.assertEqual(self.pool._accounts[uid].state, AccountState.CIRCUIT_OPEN)

    async def test_disabled_never_automatically_revives(self):
        await self.pool.set_enabled("a", False)
        self.pool._accounts["a"].state = AccountState.DISABLED
        self.pool._accounts["a"].until = 0
        lease = await self.pool.acquire()
        self.assertEqual(lease.account.id, "b")
        await lease.release(None)

    async def test_reauth_requires_explicit_new_credential(self):
        lease = await self.pool.acquire()
        uid = lease.account.id
        await lease.release(Failure(FailureKind.REAUTH, message="expired"))
        self.assertEqual(self.pool._accounts[uid].state, AccountState.REAUTH_REQUIRED)
        with self.assertRaisesRegex(RuntimeError, "没有可用账号"):
            while True:
                lease = await self.pool.acquire()
                await lease.release(Failure(FailureKind.REAUTH))

    async def test_atomic_private_state_persistence(self):
        lease = await self.pool.acquire()
        uid = lease.account.id
        await lease.release(Failure(FailureKind.RATE_LIMIT, 60))
        await self.pool.save()
        mode = (self.root / "state.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertNotIn("secret", (self.root / "state.json").read_text())
        restored = AccountPool(self.root)
        restored.load()
        self.assertEqual(restored._accounts[uid].state, AccountState.COOLING)


if __name__ == "__main__":
    unittest.main()
