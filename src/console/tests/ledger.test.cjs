// Exercise actual App effects, without a browser, with controlled fetch ordering.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const path = require('node:path')

test('refresh preserves ledger filter and stale responses cannot overwrite new selection', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/main.tsx'), 'utf8')
    .replace(/createRoot\(document\.getElementById\('root'\)!\)\.render\(<React\.StrictMode><App \/><\/React\.StrictMode>\)/, 'export { App }')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText
  const states = [], effects = [], callbacks = [], requests = [], ledgerRequests = []
  let stateIndex, effectIndex, callbackIndex
  const initial = { 0: 'usage', 5: 'plugin-a', 7: 'test-key', 8: true }
  const react = {
    useState(init) {
      const i = stateIndex++
      if (!(i in states)) states[i] = i in initial ? initial[i] : typeof init === 'function' ? init() : init
      return [states[i], value => { states[i] = typeof value === 'function' ? value(states[i]) : value }]
    },
    useRef(init) { return { current: init } },
    useCallback(fn) { const i = callbackIndex++; return callbacks[i] ?? (callbacks[i] = fn) },
    useMemo(fn) { return fn() },
    useEffect(fn, deps) { effects[effectIndex++] = { fn, deps } },
  }
  const context = { exports: {}, AbortController, sessionStorage: { getItem: () => '' },
    require(name) {
      if (name === 'react') return react
      if (name === 'react/jsx-runtime') return { jsx: () => null, jsxs: () => null }
      if (name === 'react-dom/client' || name.endsWith('.css')) return {}
      if (name === './WorkbuddyPlayground') return { WorkbuddyPlayground: 'WorkbuddyPlayground' }
      if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => key })
      throw Error(name)
    },
    fetch: async (url, options) => {
      requests.push(url)
      if (url.startsWith('/kernel/ledger')) return new Promise(resolve => ledgerRequests.push({ url, signal: options.signal, resolve }))
      return { ok: true, status: 200, json: async () => url === '/kernel/plugins' ? { plugins: [] } : url === '/v1/models' ? { data: [] } : { events: [] } }
    },
  }
  vm.runInNewContext(js, context)
  const render = () => { stateIndex = effectIndex = callbackIndex = 0; context.exports.App() }
  const flush = () => new Promise(resolve => setImmediate(resolve))
  const reply = (request, plugin) => request.resolve({ ok: true, json: async () => ({ records: [{ plugin }] }) })
  render()
  let cleanup = effects[1].fn()
  reply(ledgerRequests[0], 'plugin-a')
  await flush()
  assert.equal(states[4].records[0].plugin, 'plugin-a')
  const previousDeps = effects[1].deps
  await callbacks[0]('test-key')
  render()
  assert.notEqual(effects[1].deps.at(-1), previousDeps.at(-1), 'successful refresh invalidates the filtered query')
  cleanup()
  cleanup = effects[1].fn()
  states[5] = 'plugin-b'
  render()
  cleanup()
  cleanup = effects[1].fn()
  reply(ledgerRequests[2], 'plugin-b')
  await flush()
  reply(ledgerRequests[1], 'plugin-a')
  await flush()
  assert.equal(states[4].records[0].plugin, 'plugin-b')
  assert.ok(ledgerRequests[1].signal.aborted)
  assert.ok(!requests.includes('/kernel/ledger'), 'refresh must never publish an unfiltered ledger')
  cleanup()
})
