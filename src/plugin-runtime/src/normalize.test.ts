import { test } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeChunk, sseData } from './normalize.js'

test('normalize reasoning and tool calls', () => {
  const chunk = normalizeChunk({ choices: [{ delta: { reasoning_content: 'think', tool_calls: [{ function: { name: 'search' } }] } }] }, 'model')
  assert.equal(chunk.choices[0]?.delta.reasoning_content, 'think')
  assert.equal(chunk.choices[0]?.delta.tool_calls?.length, 1)
})

test('normalize Gemini thought to reasoning_content', () => {
  const chunk = normalizeChunk({ candidates: [{ content: { parts: [{ text: 'reason', thought: true }] } }] }, 'gemini')
  assert.equal(chunk.choices[0]?.delta.reasoning_content, 'reason')
})

test('SSE handles CRLF multiline and split frames', async () => {
  const bytes = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes.encode('data: {"a":\r\n'))
      controller.enqueue(bytes.encode('data: 1}\r\n\r\ndata: [DONE]\r\n\r\n'))
      controller.close()
    },
  })
  const frames = []
  for await (const data of sseData(new Response(stream))) frames.push(data)
  assert.deepEqual(frames, ['{"a":\n1}', '[DONE]'])
})
