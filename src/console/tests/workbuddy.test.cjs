const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const path = require('node:path')

test('WorkBuddy connection checks use only its own endpoint and show failure unless ready', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/main.tsx'), 'utf8')
    .replace(/createRoot\(document\.getElementById\('root'\)!\)\.render\(<React\.StrictMode><App \/><\/React\.StrictMode>\)/, 'export { WorkbuddyDetails }')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText
  const states = [], refs = [], calls = []
  let stateIndex, refIndex, nextResponse
  const react = {
    useState(init) { const i = stateIndex++; if (!(i in states)) states[i] = init; return [states[i], value => { states[i] = value }] },
    useRef(init) { const i = refIndex++; return refs[i] ?? (refs[i] = { current: init }) },
    useEffect() {},
  }
  const jsx = (type, props) => ({ type, props: props ?? {} })
  const context = { exports: {}, AbortController, window: { setTimeout, clearTimeout },
    require(name) {
      if (name === 'react') return react
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
      if (name === 'react-dom/client' || name.endsWith('.css')) return {}
      if (name === './WorkbuddyPlayground') return { WorkbuddyPlayground: 'WorkbuddyPlayground' }
      if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => key })
      throw Error(name)
    },
    fetch: async (url, options) => { calls.push({ url, options }); return nextResponse },
  }
  vm.runInNewContext(js, context)
  const render = () => { stateIndex = refIndex = 0; return context.exports.WorkbuddyDetails({ plugin: { name: '2api-plugin-workbuddy', status: 'running' }, apiKey: 'local-key' }) }
  const flush = () => new Promise(resolve => setImmediate(resolve))
  const button = tree => tree.props.children.find(item => item?.type === 'button')
  nextResponse = { ok: true, json: async () => ({ health: { ready: true } }) }
  button(render()).props.onClick()
  await flush()
  assert.equal(states[0], 'success')
  assert.equal(calls[0].url, '/kernel/plugins/2api-plugin-workbuddy/health/check')
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(calls[0].options.headers.Authorization, 'Bearer local-key')
  nextResponse = { ok: true, json: async () => ({ health: { ready: false } }) }
  button(render()).props.onClick()
  await flush()
  assert.equal(states[0], 'failed')
  nextResponse = { ok: false, status: 409 }
  button(render()).props.onClick()
  await flush()
  assert.equal(states[0], 'failed')
  assert.ok(calls.every(call => call.url !== '/kernel/health/check'))
})

function accountsHarness(accounts, timeout = 30000) {
  const source = fs.readFileSync(path.join(__dirname, '../src/main.tsx'), 'utf8')
    .replace(/createRoot\(document\.getElementById\('root'\)!\)\.render\(<React\.StrictMode><App \/><\/React\.StrictMode>\)/, 'export { GatewayAccounts }')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText
  const states = [], refs = [], cleanups = [], calls = [], locks = {}
  let stateIndex, refIndex, fetchImpl
  const react = {
    useState(init) { const i = stateIndex++; if (!(i in states)) states[i] = init; return [states[i], value => { states[i] = value }] },
    useRef(init) { const i = refIndex++; return refs[i] ?? (refs[i] = { current: init }) },
    useEffect(fn) { if (!cleanups.length) cleanups.push(fn()) },
  }
  const jsx = (type, props) => ({ type, props: props ?? {} })
  let nextUuid = 0
  const context = { exports: {}, AbortController, crypto: { randomUUID: () => `unique-${++nextUuid}` }, window: { setTimeout: (fn, ms) => setTimeout(fn, ms === 500 ? 0 : ms === 30000 ? timeout : ms), clearTimeout },
    require(name) {
      if (name === 'react') return react
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
      if (name === 'react-dom/client' || name.endsWith('.css')) return {}
      if (name === './WorkbuddyPlayground') return { WorkbuddyPlayground: 'WorkbuddyPlayground' }
      if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => key })
      throw Error(name)
    },
    fetch: async (url, options) => { calls.push({ url, options }); return fetchImpl(url, options) },
  }
  vm.runInNewContext(js, context)
  const render = () => { stateIndex = refIndex = 0; return context.exports.GatewayAccounts({ plugin: { name: '2api-plugin-workbuddy', status: 'running', accounts }, apiKey: 'test-local-key', actionLocks: locks, setActionLock: (uid, state) => { if (state) locks[uid] = state; else delete locks[uid]; return true } }) }
  return { states, calls, cleanups, locks, render, setFetch: fn => { fetchImpl = fn } }
}

test('original gateway accounts poll to success and distinguish unknown cached credit', async () => {
  const h = accountsHarness([{ id: 'bridge' }])
  let polls = 0
  h.setFetch(async url => url.endsWith('/runs')
    ? { ok: true, json: async () => ({ id: 'run-id' }) }
    : { ok: true, json: async () => ++polls === 1 ? { state: 'running' } : { state: 'succeeded', result: { source: 'gateway_cache', truncated: false, accounts: [{ uid: 'a', nickname: '', realm: '', cachedCredits: 0 }] } } })
  h.render().props.children.find(item => item?.type === 'button').props.onClick()
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(h.states[1], 'success')
  assert.equal(h.calls.length, 3)
  assert.equal(h.calls[0].url, '/kernel/plugins/2api-plugin-workbuddy/actions/gateway_accounts/runs')
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { accountId: 'bridge', input: {} })
  const rendered = h.render().props.children.find(item => item?.props?.role === 'region')
  const row = rendered.props.children.find(Array.isArray)[0]
  assert.equal(row.props.children[2].props.children[1], '0（可能未知）')
})

test('original gateway cache handles failures, missing accounts and unmount', async () => {
  const empty = accountsHarness([])
  const disabled = empty.render().props.children.find(item => item?.type === 'button')
  assert.equal(disabled.props.disabled, true)
  disabled.props.onClick()
  assert.equal(empty.calls.length, 0)

  const fail = accountsHarness([{ id: 'bridge' }])
  fail.setFetch(async () => ({ ok: false, status: 503 }))
  fail.render().props.children.find(item => item?.type === 'button').props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(fail.states[1], 'failed')
  assert.equal(fail.states[2], null)

  const closing = accountsHarness([{ id: 'bridge' }])
  let resolveRun
  closing.setFetch(() => new Promise(resolve => { resolveRun = resolve }))
  closing.render().props.children.find(item => item?.type === 'button').props.onClick()
  closing.cleanups[0]()
  assert.ok(closing.calls[0].options.signal.aborted)
  resolveRun({ ok: true, json: async () => ({ id: 'late' }) })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(closing.states[1], 'loading')
  assert.equal(closing.calls.length, 1)

  const timedOut = accountsHarness([{ id: 'bridge' }], 2)
  timedOut.setFetch((_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted')))))
  timedOut.render().props.children.find(item => item?.type === 'button').props.onClick()
  await new Promise(resolve => setTimeout(resolve, 15))
  assert.equal(timedOut.states[1], 'failed')
  assert.equal(timedOut.states[2], null)
})

test('usage shows vendor settlements separately and keeps unclassified amounts unknown', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/main.tsx'), 'utf8')
    .replace(/createRoot\(document\.getElementById\('root'\)!\)\.render\(<React\.StrictMode><App \/><\/React\.StrictMode>\)/, 'export { UsageView }')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText
  const jsx = (type, props) => ({ type, props: props ?? {} })
  const context = { exports: {}, require(name) {
    if (name === 'react') return {}
    if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
    if (name === 'react-dom/client' || name.endsWith('.css')) return {}
      if (name === './WorkbuddyPlayground') return { WorkbuddyPlayground: 'WorkbuddyPlayground' }
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => key })
    throw Error(name)
  } }
  vm.runInNewContext(js, context)
  const texts = node => node == null || typeof node === 'boolean' ? '' : Array.isArray(node) ? node.map(texts).join(' ') : typeof node === 'object' ? texts(node.props?.children) : String(node)
  const render = ledger => texts(context.exports.UsageView({ ledger, plugins: [], filter: 'all', setFilter() {} }))
  const base = { totals: { vendor_cost: 999 }, records: [] }
  const unknown = render(base)
  assert.match(unknown, /平台结算\s+—/)
  assert.doesNotMatch(unknown, /999/)
  const workbuddy = { plugin: '2api-plugin-workbuddy', unit: 'workbuddy_credit', dimension: 'chat', cost: '1.25' }
  const single = render({ ...base, settlements: [workbuddy], unclassified_settlements: 1 })
  assert.match(single, /1\.25\s+WorkBuddy 积分/)
  assert.match(single, /1 笔未分类/)
  const mixed = render({ ...base, settlements: [workbuddy, { plugin: '2api-plugin-other', unit: 'CNY', dimension: 'chat', cost: '2' }] })
  assert.match(mixed, /平台结算\s+分项/)
  assert.match(mixed, /1\.25\s+WorkBuddy 积分/)
  assert.match(mixed, /2\s+CNY/)
  assert.doesNotMatch(mixed, /3\.25/)
  const scientific = render({ ...base, settlements: [{ ...workbuddy, cost: '1E-7' }] })
  assert.match(scientific, /1E-7\s+WorkBuddy 积分/)
})

test('account actions isolate unknown locks, require explicit verification and use unique keys', async () => {
  const h = accountsHarness([{ id: 'bridge' }])
  let paused = false
  let outcome = 'unknown'
  h.setFetch(async url => {
    if (url.endsWith('/gateway_accounts/runs')) return { ok: true, json: async () => ({ id: 'list' }) }
    if (url.endsWith('/gateway_account_pause/runs') || url.endsWith('/gateway_account_resume/runs')) return { ok: true, json: async () => ({ id: 'change' }) }
    if (url.endsWith('/list')) return { ok: true, json: async () => ({ state: 'succeeded', result: { source: 'gateway_cache', truncated: false, accounts: [{ uid: 'uid-1', nickname: '', realm: 'cn', paused }, { uid: 'uid-2', nickname: '', realm: 'cn' }, { uid: '../bad', nickname: '', realm: '' }] } }) }
    return { ok: true, json: async () => ({ state: outcome }) }
  })
  const settle = () => new Promise(resolve => setImmediate(resolve))
  const walk = node => node == null || typeof node !== 'object' ? [] : Array.isArray(node) ? node.flatMap(walk) : [node, ...walk(node.props?.children)]
  const button = label => walk(h.render()).find(item => item.type === 'button' && (item.props['aria-label'] === label || item.props.children === label))
  const refresh = () => button('读取原网关账号缓存').props.onClick()
  refresh(); await settle()
  const pause = button('暂停 uid-1 选号')
  assert.ok(button('暂停 uid-2 选号'))
  assert.ok(button('恢复 uid-2 选号'), 'unknown pause state exposes explicit choices')
  assert.equal(button('暂停 ../bad 选号'), undefined, 'invalid UID has no mutation control')
  pause.props.onClick(); pause.props.onClick(); await settle()
  let writes = h.calls.filter(call => call.url.includes('/gateway_account_pause/runs'))
  assert.equal(writes.length, 1)
  assert.equal(JSON.parse(writes[0].options.body).idempotencyKey, 'unique-1')
  assert.equal(h.locks['uid-1'], 'unknown')
  assert.equal(button('暂停 uid-1 选号').props.disabled, true)
  assert.equal(button('暂停 uid-2 选号').props.disabled, false, 'other accounts remain available')
  paused = true
  refresh(); await settle()
  assert.equal(h.locks['uid-1'], 'unknown', 'refresh cannot resolve unknown outcome')
  assert.equal(button('恢复 uid-1 选号').props.disabled, true)
  button('已在原面板核对').props.onClick()
  assert.equal(h.locks['uid-1'], undefined)
  assert.equal(button('恢复 uid-1 选号'), undefined, 'fresh cache is required after confirmation')
  refresh(); await settle()
  outcome = 'succeeded'
  button('恢复 uid-1 选号').props.onClick(); await settle()
  writes = h.calls.filter(call => call.url.includes('/gateway_account_resume/runs'))
  assert.equal(writes.length, 1)
  assert.equal(JSON.parse(writes[0].options.body).idempotencyKey, 'unique-2')
  assert.equal(h.locks['uid-1'], 'succeeded')
  assert.equal(button('恢复 uid-1 选号').props.disabled, true)
  refresh(); await settle()
  assert.equal(h.locks['uid-1'], undefined, 'successful action requires a fresh cache')
})

test('a pending account action remains unknown after the dialog unmounts', async () => {
  const h = accountsHarness([{ id: 'bridge' }])
  let resolveAction
  h.setFetch(async url => {
    if (url.endsWith('/gateway_accounts/runs')) return { ok: true, json: async () => ({ id: 'list' }) }
    if (url.endsWith('/list')) return { ok: true, json: async () => ({ state: 'succeeded', result: { source: 'gateway_cache', truncated: false, accounts: [{ uid: 'uid-1', nickname: '', realm: '', paused: false }] } }) }
    return new Promise(resolve => { resolveAction = resolve })
  })
  const walk = node => node == null || typeof node !== 'object' ? [] : Array.isArray(node) ? node.flatMap(walk) : [node, ...walk(node.props?.children)]
  const button = name => walk(h.render()).find(item => item.type === 'button' && (item.props['aria-label'] === name || item.props.children === name))
  button('读取原网关账号缓存').props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  button('暂停 uid-1 选号').props.onClick()
  assert.equal(h.locks['uid-1'], 'running')
  h.cleanups[0]()
  resolveAction({ ok: true, json: async () => ({ id: 'late' }) })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.locks['uid-1'], 'unknown')
  assert.equal(h.calls.filter(call => call.url.includes('/gateway_account_pause/runs')).length, 1)
})

test('running account locks are restored as unknown after a page reload', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/main.tsx'), 'utf8')
    .replace(/createRoot\(document\.getElementById\('root'\)!\)\.render\(<React\.StrictMode><App \/><\/React\.StrictMode>\)/, 'export { loadActionLocks }')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText
  const context = { exports: {}, sessionStorage: { getItem: () => JSON.stringify({ 'uid-1': 'running', 'uid-2': 'succeeded', 'bad/id': 'unknown', 'uid-3': 'bad' }) }, require(name) {
    if (name === 'react') return {}
    if (name === 'react/jsx-runtime') return { jsx() {}, jsxs() {} }
    if (name === 'react-dom/client' || name.endsWith('.css')) return {}
    if (name === './WorkbuddyPlayground') return { WorkbuddyPlayground: 'WorkbuddyPlayground' }
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => key })
    throw Error(name)
  } }
  vm.runInNewContext(js, context)
  const restored = context.exports.loadActionLocks()
  assert.equal(restored['uid-1'], 'unknown')
  assert.equal(restored['uid-2'], 'succeeded')
  assert.equal(restored['bad/id'], undefined)
  assert.equal(restored['uid-3'], undefined)
  assert.equal(restored['toString'], undefined, 'built-in property names are not treated as locks')
})
