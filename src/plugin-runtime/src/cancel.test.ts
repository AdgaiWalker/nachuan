import { test } from 'node:test'
import assert from 'node:assert/strict'
import { openAIBusiness } from './openai.js'

test('completion propagates cancellation to upstream fetch', async () => {
  const originalFetch = globalThis.fetch
  let capturedSignal: AbortSignal | undefined
  globalThis.fetch = ((_input: unknown, init?: RequestInit) => {
    capturedSignal = init?.signal ?? undefined
    return new Promise<Response>((_resolve, reject) => {
      capturedSignal?.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')))
    })
  }) as typeof fetch
  const plugin = openAIBusiness([{ id: 'model' }])({ spec: { name: 'test', version: '1' }, config: { baseUrl: 'http://localhost' } })
  const controller = new AbortController()
  const pending = plugin.completion({ credential: { id: 'default', secret: {} }, model: 'model', request: { model: 'model', messages: [] } }, controller.signal)
  controller.abort()
  await assert.rejects(pending)
  assert.equal(capturedSignal?.aborted, true)
  globalThis.fetch = originalFetch
})
