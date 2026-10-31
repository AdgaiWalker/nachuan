import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from nachuan.config import ConfigStore, HostConfig
from nachuan.ledger import Ledger, UsageRecord
from nachuan.tasks import Scheduler, TaskSpec


class ProductionTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_projection_keeps_routes_and_plugins_distinct(self):
        config = HostConfig.from_dict({
            "kernel": {"routes": {"alias": ["model-a", "model-b"]}},
            "plugins": {"plugin-a": {"enabled": False, "maxInFlight": 3}},
        })
        self.assertEqual(config.routes["alias"], ("model-a", "model-b"))
        self.assertFalse(config.plugins["plugin-a"].enabled)
        self.assertEqual(config.plugins["plugin-a"].max_in_flight, 3)
        public = config.public()
        self.assertEqual(public["routes"]["alias"], ["model-a", "model-b"])
        self.assertEqual(public["plugins"]["plugin-a"]["maxInFlight"], 3)
        json.dumps(public)

    async def test_config_rolls_back_on_apply_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"kernel": {"requestTimeoutSeconds": 120}}), encoding="utf-8")
            store = ConfigStore(path)
            store.load()
            async def reject(_):
                raise RuntimeError("cannot publish")
            with self.assertRaises(RuntimeError):
                await store.update({"requestTimeoutSeconds": 1}, reject)
            self.assertEqual(json.loads(path.read_text())["kernel"]["requestTimeoutSeconds"], 120)

    async def test_first_hot_update_preserves_existing_plugin_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"plugins": {"plugin-a": {"enabled": False, "maxInFlight": 3}}}))
            store = ConfigStore(path)
            async def apply(_): pass
            await store.update({"requestTimeoutSeconds": 60}, apply)
            saved = json.loads(path.read_text())
            self.assertFalse(saved["plugins"]["plugin-a"]["enabled"])
            self.assertEqual(saved["plugins"]["plugin-a"]["maxInFlight"], 3)

    async def test_config_hot_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.load()
            applied = []
            async def apply(config): applied.append(config)
            config = await store.update({"requestTimeoutSeconds": 30, "healthIntervalSeconds": 60, "fallbackEnabled": True}, apply)
            self.assertEqual(applied[0].request_timeout_seconds, 30)
            self.assertTrue(config.fallback_enabled)

    async def test_scheduler_never_reenters_task(self):
        scheduler = Scheduler()
        gate = asyncio.Event()
        calls = 0
        async def execute(_):
            nonlocal calls
            calls += 1
            await gate.wait()
            return {}
        spec = TaskSpec("task", "plugin", "refresh", 15)
        first = asyncio.create_task(scheduler.run(spec, execute))
        await asyncio.sleep(0)
        self.assertFalse(await scheduler.run(spec, execute))
        gate.set()
        self.assertTrue(await first)
        self.assertEqual(calls, 1)
        await scheduler.close()

    async def test_ledger_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "usage.json")
            ledger.start()
            await ledger.close()
            await ledger.close()

    async def test_ledger_concurrent_conservation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "usage.json")
            ledger.start()
            async def record(index):
                return await ledger.record(UsageRecord(f"req-{index}", index, "plugin", "model", "account", False, True, 1, 1, 1, 2, "upstream"))
            results = await asyncio.gather(*(record(index) for index in range(100)))
            await ledger.close()
            self.assertTrue(all(results))
            self.assertEqual(ledger.snapshot()["totals"]["total_tokens"], 200)
            self.assertEqual(len(ledger.snapshot()["records"]), 100)


if __name__ == "__main__":
    unittest.main()
