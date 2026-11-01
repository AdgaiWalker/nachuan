"""Python 流式归一化：SSE 分帧、思考状态与用量元数据。

平台专有转换保留在 TypeScript；这里只处理通用 OpenAI 流不变量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator
import json


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    first_chunk: dict[str, Any] | None = None
    vendor: dict[str, Any] = field(default_factory=dict)

    def observe(self, chunk: dict[str, Any]) -> None:
        if self.first_chunk is None:
            self.first_chunk = chunk
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            if isinstance(usage.get("vendor"),dict): self.vendor=usage["vendor"]
            self.prompt_tokens = max(self.prompt_tokens, int(usage.get("prompt_tokens", 0) or 0))
            self.completion_tokens = max(self.completion_tokens, int(usage.get("completion_tokens", 0) or 0))
            self.total_tokens = max(self.total_tokens, int(usage.get("total_tokens", 0) or 0))


@dataclass(slots=True)
class StreamNormalizer:
    model: str
    request_id: str = ""
    created: int = field(default_factory=lambda: int(__import__("time").time()))
    in_thought: bool = False
    buffer: str = ""
    done: bool = False
    usage: Usage = field(default_factory=Usage)
    _states: dict[int, StreamNormalizer] = field(default_factory=dict)

    def data(self, data: str) -> Iterator[dict[str, Any]]:
        """输入已归一化 data 载荷；[DONE] 是幂等终止信号。"""
        if self.done:
            return
        if data.strip() == "[DONE]":
            self.done = True
            return
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            raise ValueError("流式载荷必须是 JSON 对象")
        choices = chunk.get("choices")
        emitted_content = False
        control_choices: list[dict[str, Any]] = []
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    delta = {}
                index = int(choice.get("index", 0) or 0)
                for key in ("content", "reasoning_content"):
                    if isinstance(delta.get(key), str) and delta[key]:
                        emitted_content = True
                        state = self._states.setdefault(index, StreamNormalizer(self.model, self.request_id, self.created))
                        yield from state._text(delta[key], key == "reasoning_content", chunk, index)
                remainder = {key: value for key, value in delta.items() if key not in {"content", "reasoning_content", "reasoning"}}
                if remainder or choice.get("finish_reason") is not None:
                    control = dict(choice)
                    control["delta"] = remainder
                    control_choices.append(control)
        emitted_control = False
        if control_choices:
            yield {**chunk, "choices": control_choices}
            emitted_control = True
        elif not emitted_content:
            yield chunk
            emitted_control = True
        self.usage.observe(chunk)
        if chunk.get("usage") is not None and not emitted_control:
            yield {**chunk, "choices": []}

    def _text(self, text: str, reasoning: bool, chunk: dict[str, Any], choice_index: int = 0) -> Iterator[dict[str, Any]]:
        # 思考块可能跨任意字节边界，使用缓冲保留不完整标签。
        content = self.buffer + text
        self.buffer = ""
        index = 0
        while index < len(content):
            if self.in_thought:
                marker = content.find("</think>", index)
                if marker < 0:
                    tail = content[index:]
                    keep = _partial_suffix(tail, "</think>")
                    emit = tail[:-keep] if keep else tail
                    if emit:
                        yield self._chunk(emit, True, chunk, choice_index)
                    self.buffer = tail[-keep:] if keep else ""
                    return
                if marker > index:
                    yield self._chunk(content[index:marker], True, chunk, choice_index)
                self.in_thought = False
                index = marker + len("</think>")
            else:
                marker = content.find("<think>", index)
                if marker < 0:
                    tail = content[index:]
                    keep = _partial_suffix(tail, "<think>")
                    emit = tail[:-keep] if keep else tail
                    if emit:
                        yield self._chunk(emit, reasoning, chunk, choice_index)
                    self.buffer = tail[-keep:] if keep else ""
                    return
                if marker > index:
                    yield self._chunk(content[index:marker], reasoning, chunk, choice_index)
                self.in_thought = True
                index = marker + len("<think>")

    def _chunk(self, text: str, reasoning: bool, source: dict[str, Any], index: int = 0) -> dict[str, Any]:
        return {
            "id": source.get("id") or self.request_id,
            "object": "chat.completion.chunk",
            "created": source.get("created") or self.created,
            "model": source.get("model") or self.model,
            "choices": [{"index": index, "delta": {"reasoning_content" if reasoning else "content": text}, "finish_reason": None}],
        }

    def finish(self) -> Iterator[dict[str, Any]]:
        for index, state in self._states.items():
            if state.buffer:
                yield state._chunk(state.buffer, state.in_thought, {}, index)
                state.buffer = ""
        if self.buffer:
            yield self._chunk(self.buffer, self.in_thought, {})
            self.buffer = ""


def _partial_suffix(text: str, marker: str) -> int:
    return next((length for length in range(1, len(marker)) if text.endswith(marker[:length])), 0)
