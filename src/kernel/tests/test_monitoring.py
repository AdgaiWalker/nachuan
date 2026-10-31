import time
import unittest

from nachuan.monitoring import Monitor, sanitize


class MonitorTests(unittest.TestCase):
    def test_request_metrics_and_token_usage(self):
        monitor = Monitor()
        request_id = monitor.start("plugin-a", "model-a", False)
        monitor.finish("plugin-a", request_id, time.monotonic(), model="model-a", result={"usage": {"total_tokens": 42}})
        data = monitor.snapshot()
        self.assertEqual(data["totals"]["requests"], 1)
        self.assertEqual(data["totals"]["success_rate"], 100)
        self.assertEqual(data["totals"]["tokens"], 42)
        self.assertEqual(data["totals"]["in_flight"], 0)

    def test_failure_and_isolated_plugins(self):
        monitor = Monitor()
        first = monitor.start("plugin-a", "model-a", True)
        second = monitor.start("plugin-b", "model-b", False)
        monitor.finish("plugin-a", first, time.monotonic(), model="model-a", error="HTTP 429")
        monitor.finish("plugin-b", second, time.monotonic(), model="model-b")
        self.assertEqual(monitor.snapshot()["plugins"]["plugin-a"]["failures"], 1)
        self.assertEqual(monitor.snapshot()["plugins"]["plugin-b"]["successes"], 1)

    def test_secrets_never_enter_event_stream(self):
        monitor = Monitor()
        fake_key = "sk-" + "test-placeholder-key"
        monitor.log("error", "plugin-a", f"Bearer secret-token\n {fake_key}")
        text = str(monitor.snapshot()["events"])
        self.assertNotIn("secret-token", text)
        self.assertNotIn(fake_key, text)
        self.assertNotIn("\n", monitor.snapshot()["events"][0]["message"])

    def test_event_history_is_bounded(self):
        monitor = Monitor(event_limit=2)
        for index in range(3):
            monitor.log("info", "plugin", f"event {index}")
        self.assertEqual(len(monitor.snapshot()["events"]), 2)


if __name__ == "__main__":
    unittest.main()
