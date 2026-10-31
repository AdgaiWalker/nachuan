import json
import unittest

from nachuan.streaming import StreamNormalizer


def chunk(delta, **extra):
    return json.dumps({"id": "id", "created": 1, "model": "model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}], **extra})


class StreamingTests(unittest.TestCase):
    def test_think_tags_split_across_packets(self):
        normalizer = StreamNormalizer("model")
        output = []
        for value in ("before<th", "ink>reason</th", "ink>after"):
            output.extend(normalizer.data(chunk({"content": value})))
        output.extend(normalizer.finish())
        self.assertEqual(output[0]["choices"][0]["delta"]["content"], "before")
        self.assertEqual(output[1]["choices"][0]["delta"]["reasoning_content"], "reason")
        self.assertEqual(output[2]["choices"][0]["delta"]["content"], "after")

    def test_reasoning_field_preserved(self):
        output = list(StreamNormalizer("model").data(chunk({"reasoning_content": "thinking"})))
        self.assertEqual(output[0]["choices"][0]["delta"]["reasoning_content"], "thinking")

    def test_tool_calls_pass_through(self):
        call = {"index": 0, "function": {"name": "search", "arguments": "{}"}}
        output = list(StreamNormalizer("model").data(chunk({"tool_calls": [call]})))
        self.assertEqual(output[0]["choices"][0]["delta"]["tool_calls"], [call])

    def test_done_is_idempotent(self):
        normalizer = StreamNormalizer("model")
        self.assertEqual(list(normalizer.data("[DONE]")), [])
        self.assertEqual(list(normalizer.data("[DONE]")), [])
        self.assertEqual(list(normalizer.data(chunk({"content": "late"}))), [])

    def test_usage_is_emitted_exactly_once(self):
        normalizer = StreamNormalizer("model")
        output = list(normalizer.data(chunk({}, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})))
        self.assertEqual(normalizer.usage.total_tokens, 15)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["usage"]["total_tokens"], 15)

    def test_finish_reason_is_preserved_with_content(self):
        normalizer = StreamNormalizer("model")
        payload = json.dumps({"id": "id", "created": 1, "model": "model", "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]})
        output = list(normalizer.data(payload))
        self.assertEqual(output[0]["choices"][0]["delta"]["content"], "done")
        self.assertEqual(output[-1]["choices"][0]["finish_reason"], "stop")

    def test_content_and_tool_calls_are_not_duplicated(self):
        normalizer = StreamNormalizer("model")
        call = {"index": 0, "function": {"name": "search", "arguments": "{}"}}
        payload = json.dumps({"id": "id", "created": 1, "model": "model", "choices": [{"index": 0, "delta": {"content": "text", "tool_calls": [call]}, "finish_reason": None}]})
        output = list(normalizer.data(payload))
        self.assertEqual(sum("content" in item["choices"][0]["delta"] for item in output), 1)
        self.assertEqual(output[-1]["choices"][0]["delta"]["tool_calls"], [call])


if __name__ == "__main__":
    unittest.main()
