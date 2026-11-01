const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const path = require('node:path')

function harness(fetchImpl) {
  const source = fs.readFileSync(path.join(__dirname, '../src/WorkbuddyPlayground.tsx'), 'utf8')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
  const states = [], refs = [], effects = []
  let stateIndex, refIndex, effectIndex
  const jsx = (type, props) => ({ type, props: props ?? {} })
  const react = {
    useState(initial) {
      const index = stateIndex++
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      return [states[index], update => { states[index] = typeof update === 'function' ? update(states[index]) : update }]
    },
    useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }) },
    useEffect(fn) { const index = effectIndex++; if (!effects[index]) effects[index] = { fn, cleanup: null } },
  }
  const context = {
    exports: {}, AbortController, TextDecoder, Uint8Array, fetch: fetchImpl,
    require(name) {
      if (name === 'react') return react
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
      throw Error(name)
    },
  }
  vm.runInNewContext(js, context)
  const props = { apiKey: 'local-secret', models: [{ id: 'workbuddy:glm-5.2', owned_by: '2api-plugin-workbuddy' }, { id: 'other:model', owned_by: 'other' }] }
  function render() {
    stateIndex = refIndex = effectIndex = 0
    const tree = context.exports.WorkbuddyPlayground(props)
    for (const effect of effects) if (effect && !effect.cleanup) effect.cleanup = effect.fn() || (() => {})
    return tree
  }
  const all = (node, type) => node == null ? [] : Array.isArray(node) ? node.flatMap(child => all(child, type)) : typeof node === 'object' ? [...(node.type === type ? [node] : []), ...all(node.props.children, type)] : []
  const text = node => node == null || typeof node === 'boolean' ? '' : Array.isArray(node) ? node.map(text).join(' ') : typeof node === 'object' ? text(node.props.children) : String(node)
  function enter(value = '你好') {
    const input = all(render(), 'textarea')[0]
    input.props.onChange({ target: { value } })
  }
  function submit() {
    return all(render(), 'form')[0].props.onSubmit({ preventDefault() {} })
  }
  function cleanup() { for (const effect of effects) effect?.cleanup?.() }
  return { render, enter, submit, cleanup, all, text, exports: context.exports, states }
}

function streaming(chunks, { status = 200, onCancel = () => {} } = {}) {
  let index = 0
  return { ok: status === 200, status, body: { getReader: () => ({
    async read() { return index < chunks.length ? { value: chunks[index++], done: false } : { done: true } },
    async cancel() { onCancel() }, releaseLock() {},
  }) } }
}
const encoder = new TextEncoder()

function wire(text) { return encoder.encode(text) }

test('renders selected WorkBuddy response through split UTF-8 SSE and requires DONE', async () => {
  const bytes = wire('data: {"choices":[{"delta":{"content":"你好"}}]}\r\n\r\ndata: [DONE]\r\n\r\n')
  let request, cancelled = false
  const h = harness(async (url, options) => { request = { url, options }; return streaming([bytes.slice(0, 33), bytes.slice(33, 35), bytes.slice(35)], { onCancel: () => { cancelled = true } }) })
  h.enter()
  await h.submit()
  const tree = h.render()
  assert.equal(request.url, '/v1/chat/completions')
  assert.equal(request.options.headers.Authorization, 'Bearer local-secret')
  const body = JSON.parse(request.options.body)
  assert.equal(body.model, 'workbuddy:glm-5.2')
  assert.equal(body.stream, true)
  assert.equal(body.max_tokens, 256)
  assert.doesNotMatch(request.options.body, /local-secret/)
  assert.match(h.text(tree), /你好/)
  assert.match(h.text(tree), /回复完成/)
  assert.ok(cancelled)
  assert.equal(h.all(tree, 'option').length, 1)
  h.cleanup()
})

test('reports SSE error and unexpected EOF without a false success', async () => {
  for (const bytes of [
    wire('data: {"choices":[{"delta":{"content":"部分"}}]}\n\ndata: {"error":{"message":"private"}}\n\ndata: [DONE]\n\n'),
    wire('data: {"choices":[{"delta":{"content":"部分"}}]}\n\n'),
    wire('data: not-json\n\n'),
  ]) {
    const h = harness(async () => streaming([bytes]))
    h.enter()
    await h.submit()
    const text = h.text(h.render())
    assert.match(text, /流式/)
    assert.doesNotMatch(text, /回复完成|private/)
    h.cleanup()
  }
})

test('reports an HTTP authentication error without exposing the local key', async () => {
  const h = harness(async () => streaming([], { status: 401 }))
  h.enter()
  await h.submit()
  const text = h.text(h.render())
  assert.match(text, /HTTP 401/)
  assert.doesNotMatch(text, /local-secret|回复完成/)
  h.cleanup()
})

test('cancelling or unmounting aborts a pending fetch', async () => {
  for (const mode of ['cancel', 'unmount']) {
    let signal
    const h = harness(async (_url, options) => {
      signal = options.signal
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))))
    })
    h.enter()
    const pending = h.submit()
    if (mode === 'cancel') {
      const button = h.all(h.render(), 'button').find(item => h.text(item) === '取消请求')
      button.props.onClick()
    } else h.cleanup()
    await pending
    assert.ok(signal.aborted)
    if (mode === 'cancel') assert.match(h.text(h.render()), /请求已取消/)
    h.cleanup()
  }
})

test('SSE decoder joins multiline data and handles end without trailing blank line', () => {
  const h = harness(() => {})
  const parser = new h.exports.SseDecoder()
  const result = [...parser.push(wire('data: {"a":\r\ndata: 1}\r\n\r\n')), ...parser.push(wire('data: [DONE]'), true)]
  assert.deepEqual(result, ['{"a":\n1}', '[DONE]'])
  const other = new h.exports.SseDecoder()
  const split = [...other.push(wire('data: x\r')), ...other.push(wire('\n\r')), ...other.push(wire('\ndata: y\r\r')), ...other.push(new Uint8Array(), true)]
  assert.deepEqual(split, ['x', 'y'])
})
