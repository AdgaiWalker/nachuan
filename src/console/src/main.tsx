import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { WorkbuddyPlayground } from './WorkbuddyPlayground'
import {
  Activity, ArrowUpRight, Box, Check, ChevronRight, Circle, Clock3,
  Copy, Cpu, KeyRound, LayoutDashboard, LoaderCircle, Menu, MoreHorizontal, ReceiptText,
  Plus, RefreshCw, Search, Settings, ShieldCheck, UsersRound, X, Zap,
} from 'lucide-react'
import './styles.css'

type Plugin = {
  name: string
  version: string
  status: 'running' | 'disabled' | 'fault' | 'credential_missing' | 'stopped'
  enabled: boolean
  fault: string | null
  models: number
  accounts: Array<{ id: string; state: string; in_flight: number; total_requests: number }>
  health?: { ready: boolean; last_checked: number; last_success: number; error: string }
}
type Model = { id: string; owned_by: string; max_output_tokens?: number }
type MonitorEvent = { id: string; timestamp: number; level: string; plugin: string; message: string; model?: string; duration_ms?: number; tokens?: number; account?: string }
type MonitorData = { uptime_seconds: number; totals: { requests?: number; successes?: number; failures?: number; in_flight?: number; tokens?: number; success_rate: number }; plugins: Record<string, { requests: number; successes: number; failures: number; in_flight: number; tokens: number; average_latency_ms: number; average_ttft_ms: number; success_rate: number }>; events: MonitorEvent[] }
type UsageRecord = { request_id: string; timestamp: number; plugin: string; model: string; account: string; success: boolean; duration_ms: number; total_tokens: number; prompt_tokens: number; completion_tokens: number; usage_source: string; vendor_cost?: number; vendor_unit?: string; vendor_remaining?: number }
type Settlement = { plugin: string; unit: string; dimension: string; cost: string }
type LedgerData = { totals: { requests?: number; prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; vendor_cost?: number; success_rate: number }; records: UsageRecord[]; settlements?: Settlement[]; unclassified_settlements?: number }
type GatewayAccount = { uid: string; nickname: string; realm: string; cachedCredits?: number; cachedCreditsTotal?: number; disabled?: boolean; paused?: boolean; cooling?: boolean }
type GatewayAccountsResult = { source: 'gateway_cache'; truncated: boolean; accounts: GatewayAccount[] }
type AccountActionLock = 'running' | 'succeeded' | 'unknown'
const ACTION_LOCK_STORAGE = 'nachuan.workbuddy.action-locks'
function loadActionLocks(): Record<string, AccountActionLock> {
  try {
    const data = JSON.parse(sessionStorage.getItem(ACTION_LOCK_STORAGE) || '{}')
    if (!data || typeof data !== 'object' || Array.isArray(data)) return Object.create(null)
    return Object.assign(Object.create(null), Object.fromEntries(Object.entries(data).filter(([uid, state]) => /^[A-Za-z0-9_-]{1,64}$/.test(uid) && (state === 'running' || state === 'unknown' || state === 'succeeded')).map(([uid, state]) => [uid, state === 'running' ? 'unknown' : state])))
  } catch { return Object.create(null) }
}
type View = 'overview' | 'accounts' | 'models' | 'monitor' | 'usage' | 'settings' | 'playground'

const nav = [
  { id: 'overview' as View, label: '概览', icon: LayoutDashboard },
  { id: 'accounts' as View, label: '账号与凭证', icon: UsersRound },
  { id: 'models' as View, label: '模型', icon: Box },
  { id: 'playground' as View, label: 'WorkBuddy 试用', icon: Zap },
  { id: 'monitor' as View, label: '监控', icon: Activity },
  { id: 'usage' as View, label: '用量', icon: ReceiptText },
]

function BrandMark() {
  return <div className="brand-mark"><svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m4 5 8 5 8-5M4 12l8 5 8-5M4 19l8 5 8-5" /></svg></div>
}

function App() {
  const [view, setView] = useState<View>('overview')
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [models, setModels] = useState<Model[]>([])
  const [monitor, setMonitor] = useState<MonitorData | null>(null)
  const [ledger, setLedger] = useState<LedgerData | null>(null)
  const [usagePlugin, setUsagePlugin] = useState('all')
  const [eventFilter, setEventFilter] = useState('all')
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem('nachuan.key') ?? '')
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [formError, setFormError] = useState('')
  const [authOpen, setAuthOpen] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')
  const [mobileNav, setMobileNav] = useState(false)
  const [search, setSearch] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [selectedPlugin, setSelectedPlugin] = useState('')
  const [accountId, setAccountId] = useState('')
  const [credential, setCredential] = useState('')
  const [adding, setAdding] = useState(false)
  const [copied, setCopied] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [ledgerRevision, setLedgerRevision] = useState(0)
  const [detailPlugin, setDetailPlugin] = useState('')
  const [actionLocks, setActionLocks] = useState<Record<string, AccountActionLock>>(loadActionLocks)
  const actionLocksRef = useRef(actionLocks)
  const setActionLock = (uid: string, lock: AccountActionLock | null): boolean => {
    const updated: Record<string, AccountActionLock> = Object.assign(Object.create(null), actionLocksRef.current)
    if (lock) updated[uid] = lock
    else delete updated[uid]
    try { sessionStorage.setItem(ACTION_LOCK_STORAGE, JSON.stringify(updated)) } catch { return false }
    actionLocksRef.current = updated
    setActionLocks(updated)
    return true
  }

  const load = useCallback(async (key: string) => {
    setLoading(true)
    setError('')
    try {
      const headers = { Authorization: `Bearer ${key}` }
      const [pluginsResponse, modelsResponse, monitorResponse] = await Promise.all([
        fetch('/kernel/plugins', { headers }),
        fetch('/v1/models', { headers }),
        fetch('/kernel/monitor', { headers }),
      ])
      if (pluginsResponse.status === 401) {
        setConnected(false)
        setAuthOpen(true)
        return
      }
      if (!pluginsResponse.ok || !modelsResponse.ok) throw new Error('无法连接内核')
      setPlugins((await pluginsResponse.json()).plugins)
      setModels((await modelsResponse.json()).data)
      if (monitorResponse.ok) setMonitor(await monitorResponse.json())
      setLedgerRevision(current => current + 1)
      setConnected(true)
    } catch {
      setConnected(false)
      setError('无法连接纳川内核')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load(apiKey) }, [apiKey, load])

  useEffect(() => {
    if (!connected || view !== 'usage') return
    const controller = new AbortController()
    setLedger(null)
    void fetch(`/kernel/ledger?plugin=${encodeURIComponent(usagePlugin === 'all' ? '' : usagePlugin)}`, {
      headers: { Authorization: `Bearer ${apiKey}` }, signal: controller.signal,
    }).then(async response => {
      if (!response.ok) throw new Error('无法读取用量账本')
      const data = await response.json()
      if (!controller.signal.aborted) setLedger(data)
    }).catch(() => {
      if (!controller.signal.aborted) setError('无法读取用量账本')
    })
    return () => controller.abort()
  }, [apiKey, connected, view, usagePlugin, ledgerRevision])

  useEffect(() => {
    if (!connected) return
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch('/kernel/monitor', { headers: { Authorization: `Bearer ${apiKey}` } })
        if (response.ok) setMonitor(await response.json())
      } catch { /* 下一轮继续 */ }
    }, 5000)
    return () => window.clearInterval(timer)
  }, [apiKey, connected])

  useEffect(() => {
    if (!connected || view !== 'monitor') return
    let source: EventSource | undefined
    let cancelled = false
    void fetch('/kernel/events/ticket', { method: 'POST', headers: { Authorization: `Bearer ${apiKey}` } }).then(response => response.json()).then(data => {
      if (cancelled) return
      source = new EventSource(`/kernel/events?ticket=${encodeURIComponent(data.ticket)}`)
      source.onmessage = event => {
        const item = JSON.parse(event.data) as MonitorEvent
        setMonitor(current => current ? { ...current, events: [item, ...current.events.filter(existing => existing.id !== item.id)].slice(0, 300) } : current)
      }
    }).catch(() => undefined)
    return () => { cancelled = true; source?.close() }
  }, [apiKey, connected, view])

  const saveKey = (event: React.FormEvent) => {
    event.preventDefault()
    const key = keyDraft.trim()
    if (!key) return
    sessionStorage.setItem('nachuan.key', key)
    setApiKey(key)
    setAuthOpen(false)
  }

  const addAccount = async (event: React.FormEvent) => {
    event.preventDefault()
    setAdding(true)
    setFormError('')
    try {
      const response = await fetch(`/kernel/plugins/${encodeURIComponent(selectedPlugin)}/accounts`, {
        method: 'POST', headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: accountId.trim(), apiKey: credential }),
      })
      const result = await response.json()
      if (!response.ok) {
        const code = typeof result.error === 'object' && result.error ? result.error.code : ''
        throw new Error(code === 'account_exists' ? '账号名称已存在' : code === 'invalid_request' ? '账号名称或密钥格式不正确' : '添加失败，请检查插件状态后重试')
      }
      setAddOpen(false)
      setAccountId('')
      setCredential('')
      await load(apiKey)
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : '添加失败')
    } finally {
      setAdding(false)
    }
  }

  const navigate = (next: View) => { setView(next); setMobileNav(false); setSearch('') }
  const filteredPlugins = useMemo(() => plugins.filter(plugin => plugin.name.toLowerCase().includes(search.toLowerCase())), [plugins, search])
  const filteredModels = useMemo(() => models.filter(model => model.id.toLowerCase().includes(search.toLowerCase())), [models, search])
  const accounts = plugins.reduce((total, plugin) => total + plugin.accounts.length, 0)
  const healthy = plugins.filter(plugin => plugin.status === 'running').length
  const title = view === 'settings' ? '设置' : nav.find(item => item.id === view)?.label
  const selectedDetail = plugins.find(plugin => plugin.name === detailPlugin)

  return <div className="app-shell">
    {mobileNav && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setMobileNav(false)} />}
    <aside className={`sidebar ${mobileNav ? 'open' : ''}`}>
      <div className="brand"><BrandMark /><span>纳川</span><span className="brand-version">Core</span></div>
      <nav className="navigation">
        <div className="nav-caption">工作空间</div>
        {nav.map(item => <button key={item.id} className={`nav-item ${view === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)}><item.icon size={18} strokeWidth={1.8} /><span>{item.label}</span>{item.id === 'accounts' && accounts > 0 && <span className="nav-count">{accounts}</span>}</button>)}
        <div className="nav-caption second">系统</div>
        <button className={`nav-item ${view === 'settings' ? 'active' : ''}`} onClick={() => navigate('settings')}><Settings size={18} strokeWidth={1.8} /><span>设置</span></button>
      </nav>
      <div className="sidebar-bottom">
        <div className="kernel-status"><span className={`status-dot ${connected ? 'online' : ''}`} /><span>Python 内核</span><span className="kernel-version">v1</span></div>
        <div className="profile"><div className="avatar">川</div><div><strong>本地工作空间</strong><span>个人专用</span></div><button className="icon-button" onClick={() => setMenuOpen(!menuOpen)} aria-label="工作空间菜单"><MoreHorizontal size={18} /></button></div>
        {menuOpen && <div className="profile-menu"><button onClick={() => { sessionStorage.removeItem('nachuan.key'); setApiKey(''); setAuthOpen(true); setMenuOpen(false) }}><KeyRound size={15} />更换访问密钥</button></div>}
      </div>
    </aside>

    <main className="main">
      <header className="topbar"><button className="icon-button mobile-menu" onClick={() => setMobileNav(true)} aria-label="打开导航"><Menu size={20} /></button><div className="breadcrumb"><span>纳川</span><ChevronRight size={13} /><strong>{title}</strong></div><div className="topbar-actions"><button className="icon-button" onClick={() => void load(apiKey)} aria-label="刷新"><RefreshCw size={17} className={loading ? 'spin' : ''} /></button><button className="primary-button compact" onClick={() => { setSelectedPlugin(''); setFormError(''); setAddOpen(true) }}><Plus size={16} />添加账号</button></div></header>
      <div className="content">
        {error && <div className="error-banner" role="alert"><Circle size={8} fill="currentColor" />{error}<button onClick={() => setError('')} aria-label="关闭"><X size={15} /></button></div>}
        {view === 'overview' && <>
          <section className="heading"><div><h1>概览</h1><p>统一管理你的模型服务。</p></div></section>
          <section className="metrics">
            <article><div className="metric-label"><span>可用插件</span><Zap size={17} /></div><strong>{healthy}<span> / {plugins.length}</span></strong><small>{plugins.length ? '运行中的插件' : '尚未连接'}</small></article>
            <article><div className="metric-label"><span>聚合模型</span><Box size={17} /></div><strong>{models.length}</strong><small>全部可用模型</small></article>
            <article><div className="metric-label"><span>账号</span><UsersRound size={17} /></div><strong>{accounts}</strong><small>已接入凭证</small></article>
            <article><div className="metric-label"><span>内核状态</span><Activity size={17} /></div><strong className="metric-status">{connected ? '正常' : '离线'}</strong><small><span className={`status-dot ${connected ? 'online' : ''}`} />Python 微内核</small></article>
          </section>
          <section className="section-block"><div className="section-heading"><h2>插件</h2><button className="text-button" onClick={() => navigate('accounts')}>查看全部<ChevronRight size={14} /></button></div><div className="panel plugin-panel"><div className="table-header"><span>插件</span><span>状态</span><span>模型</span><span>账号</span><span /></div>{filteredPlugins.map(plugin => <PluginRow key={plugin.name} plugin={plugin} onDetails={setDetailPlugin} />)}</div></section>
          <section className="section-block"><div className="section-heading"><h2>快捷操作</h2></div><div className="quick-grid"><button onClick={() => { setSelectedPlugin(''); setFormError(''); setAddOpen(true) }}><span className="quick-icon"><Plus size={19} /></span><span><strong>添加账号</strong><small>接入新的上游凭证</small></span><ChevronRight size={16} /></button><button onClick={() => navigate('models')}><span className="quick-icon"><Cpu size={19} /></span><span><strong>浏览模型</strong><small>查看已聚合的模型</small></span><ChevronRight size={16} /></button><button onClick={() => navigate('settings')}><span className="quick-icon"><KeyRound size={19} /></span><span><strong>接入信息</strong><small>API 地址与访问密钥</small></span><ChevronRight size={16} /></button></div></section>
        </>}
        {view === 'accounts' && <>
          <section className="heading"><div><h1>账号与凭证</h1><p>管理插件的上游账号。</p></div><button className="primary-button" onClick={() => { setSelectedPlugin(''); setFormError(''); setAddOpen(true) }}><Plus size={16} />添加账号</button></section>
          <SearchField value={search} onChange={setSearch} placeholder="搜索插件" />
          <div className="panel plugin-panel"><div className="table-header"><span>插件</span><span>状态</span><span>模型</span><span>账号</span><span /></div>{filteredPlugins.map(plugin => <PluginRow key={plugin.name} plugin={plugin} onDetails={setDetailPlugin} />)}</div>
        </>}
        {view === 'models' && <>
          <section className="heading"><div><h1>模型</h1><p>来自已就绪插件的统一模型目录。</p></div></section>
          <SearchField value={search} onChange={setSearch} placeholder="搜索模型" />
          {filteredModels.length ? <div className="model-grid">{filteredModels.map(model => <article className="model-card" key={model.id}><div className="model-symbol"><Box size={20} /></div><strong>{model.id}</strong><span>{model.owned_by}</span><div className="model-card-bottom"><span>{model.max_output_tokens ? `${(model.max_output_tokens / 1024).toLocaleString()}K` : 'OpenAI'}</span><ArrowUpRight size={16} /></div></article>)}</div> : <EmptyState icon={Box} title={search ? '没有匹配的模型' : '还没有可用模型'} text={search ? '尝试其他关键词' : '添加账号后，模型会显示在这里'} action={!search && <button className="secondary-button" onClick={() => { setFormError(''); setAddOpen(true) }}><Plus size={15} />添加账号</button>} />}
        </>}
        {view === 'playground' && <WorkbuddyPlayground apiKey={apiKey} models={models} />}
        {view === 'usage' && <UsageView ledger={ledger} plugins={plugins} filter={usagePlugin} setFilter={setUsagePlugin} />}
        {view === 'monitor' && <MonitorView monitor={monitor} plugins={plugins} filter={eventFilter} setFilter={setEventFilter} />}
        {view === 'settings' && <>
          <section className="heading"><div><h1>设置</h1><p>本地网关接入信息。</p></div></section>
          <section className="settings-panel panel"><div className="setting-row"><div><strong>API 地址</strong><span>OpenAI 兼容接口</span></div><div className="copy-field"><code>http://127.0.0.1:7865/v1</code><button className="icon-button" aria-label="复制 API 地址" onClick={async () => { await navigator.clipboard.writeText('http://127.0.0.1:7865/v1'); setCopied(true); setTimeout(() => setCopied(false), 1800) }}>{copied ? <Check size={16} /> : <Copy size={16} />}</button></div></div><div className="setting-row"><div><strong>访问密钥</strong><span>保存在当前浏览器会话</span></div><button className="secondary-button" onClick={() => { setKeyDraft(''); setAuthOpen(true) }}><KeyRound size={15} />{apiKey ? '更换密钥' : '设置密钥'}</button></div><div className="setting-row"><div><strong>内核</strong><span>Python 宿主 · TypeScript 业务插件</span></div><span className="subtle-badge"><ShieldCheck size={14} />本地运行</span></div></section>
        </>}
      </div>
    </main>

    {authOpen && <Modal onClose={() => setAuthOpen(false)}><div className="modal-icon"><KeyRound size={22} /></div><h2>连接纳川</h2><p>输入 API 访问密钥。</p><form onSubmit={saveKey}><label>访问密钥<input autoFocus type="password" value={keyDraft} onChange={event => setKeyDraft(event.target.value)} placeholder="API Key" required /></label><button className="primary-button full" type="submit">连接</button></form></Modal>}
    {addOpen && <Modal onClose={() => { setAddOpen(false); setCredential('') }}><div className="modal-icon"><Plus size={22} /></div><h2>添加账号</h2><p>{selectedPlugin === '2api-plugin-workbuddy' ? '连接已有的本机 WorkBuddy 网关。原有平台账号和额度仍由 WorkBuddy 管理。' : '凭证仅保存在对应插件目录。'}</p>{formError && <div className="form-error" role="alert">{formError}</div>}<form onSubmit={addAccount}><label>插件<select value={selectedPlugin} onChange={event => { setSelectedPlugin(event.target.value); setCredential(''); setFormError('') }} required><option value="" disabled>选择插件</option>{plugins.map(plugin => <option key={plugin.name} value={plugin.name}>{plugin.name}</option>)}</select></label><label>账号名称<input value={accountId} onChange={event => setAccountId(event.target.value)} placeholder="账号标识" required pattern="[A-Za-z0-9._-]+" /></label><label>{selectedPlugin === '2api-plugin-workbuddy' ? '本地 WorkBuddy 网关 API Key' : '上游 API Key'}<input type="password" value={credential} onChange={event => setCredential(event.target.value)} placeholder={selectedPlugin === '2api-plugin-workbuddy' ? '输入本地网关访问密钥' : '输入上游访问密钥'} autoComplete="off" required /></label>{selectedPlugin === '2api-plugin-workbuddy' && <p className="form-hint">填写 7863 网关的 API Key，而非 WorkBuddy 平台账号或登录凭证。</p>}<button className="primary-button full" type="submit" disabled={adding}>{adding ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}添加账号</button></form></Modal>}
    {selectedDetail && <Modal onClose={() => setDetailPlugin('')}><div className="modal-icon"><Zap size={22} /></div><h2>{selectedDetail.name.replace('2api-plugin-', '')}</h2><p>插件状态：{selectedDetail.status === 'running' ? '运行中' : selectedDetail.status === 'credential_missing' ? '待配置' : selectedDetail.status === 'disabled' ? '已停用' : '异常'}。</p>{selectedDetail.name === '2api-plugin-workbuddy' && <WorkbuddyDetails plugin={selectedDetail} apiKey={apiKey} actionLocks={actionLocks} setActionLock={setActionLock} />}<button className="primary-button full" onClick={() => { setSelectedPlugin(selectedDetail.name); setFormError(''); setCredential(''); setDetailPlugin(''); setAddOpen(true) }}><Plus size={16} />添加账号</button></Modal>}
  </div>
}

function UsageView({ ledger, plugins, filter, setFilter }: { ledger: LedgerData | null; plugins: Plugin[]; filter: string; setFilter: (value: string) => void }) {
  const settlements = (Array.isArray(ledger?.settlements) ? ledger.settlements : []).filter(item => typeof item.cost === 'string' && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(item.cost) && typeof item.unit === 'string' && item.unit !== '')
  const unclassified = ledger?.unclassified_settlements ?? 0
  const unitLabel = (unit: string) => unit === 'workbuddy_credit' ? 'WorkBuddy 积分' : unit
  return <>
    <section className="heading"><div><h1>用量</h1><p>Token 与上游结算记录。</p></div><select className="event-filter" value={filter} onChange={event => setFilter(event.target.value)} aria-label="按插件筛选用量"><option value="all">全部插件</option>{plugins.map(plugin => <option key={plugin.name} value={plugin.name}>{plugin.name.replace('2api-plugin-', '')}</option>)}</select></section>
    <section className="metrics">
      <article><div className="metric-label"><span>输入 Token</span><ArrowUpRight size={17} /></div><strong>{formatNumber(ledger?.totals.prompt_tokens ?? 0)}</strong><small>上游报告</small></article>
      <article><div className="metric-label"><span>输出 Token</span><Zap size={17} /></div><strong>{formatNumber(ledger?.totals.completion_tokens ?? 0)}</strong><small>上游报告</small></article>
      <article><div className="metric-label"><span>总 Token</span><ReceiptText size={17} /></div><strong>{formatNumber(ledger?.totals.total_tokens ?? 0)}</strong><small>{ledger?.totals.requests ?? 0} 次请求</small></article>
      <article><div className="metric-label"><span>平台结算</span><ReceiptText size={17} /></div><strong>{settlements.length === 1 ? settlements[0].cost : settlements.length > 1 ? '分项' : '—'}</strong><small>{settlements.length === 1 ? unitLabel(settlements[0].unit) : settlements.length > 1 ? '按平台和单位分别列示' : '无已分类结算'}{unclassified > 0 ? ` · ${unclassified} 笔未分类` : ''}</small></article>
    </section>
    {settlements.length > 0 && <section className="section-block"><div className="section-heading"><h2>结算分项</h2></div><div className="panel settlement-list">{settlements.map((item, index) => <div className="settlement-row" key={`${item.plugin}-${item.unit}-${item.dimension}-${index}`}><div><strong>{item.plugin.replace('2api-plugin-', '')}</strong><span>{item.dimension || '未指定维度'}</span></div><span>{item.cost} {unitLabel(item.unit)}</span></div>)}</div></section>}
    <section className="section-block"><div className="section-heading"><h2>用量明细</h2></div><div className="panel usage-panel"><div className="usage-header"><span>模型 / 账号</span><span>Token</span><span>耗时</span><span>状态</span><span>时间</span></div>{ledger?.records.length ? ledger.records.map(record => <div className="usage-row" key={record.request_id}><div className="usage-model"><strong>{record.model}</strong><span>{record.plugin.replace('2api-plugin-', '')}{record.account ? ` · ${record.account}` : ''}</span></div><span className="numeric">{record.total_tokens.toLocaleString()}</span><span className="numeric">{record.duration_ms} ms</span><span className={`usage-status ${record.success ? '' : 'failed'}`}>{record.success ? '成功' : '失败'}</span><time>{new Date(record.timestamp * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time></div>) : <div className="event-empty">暂无用量记录</div>}</div></section>
  </>
}

function MonitorView({ monitor, plugins, filter, setFilter }: { monitor: MonitorData | null; plugins: Plugin[]; filter: string; setFilter: (filter: string) => void }) {
  const events = (monitor?.events ?? []).filter(event => filter === 'all' || filter === 'errors' && event.level === 'error' || filter === event.plugin)
  return <>
    <section className="heading"><div><h1>监控</h1><p>内核运行状态与请求事件。</p></div><span className="live-badge"><span className="status-dot online" />实时</span></section>
    <section className="metrics">
      <article><div className="metric-label"><span>总请求</span><Activity size={17} /></div><strong>{monitor?.totals.requests ?? 0}</strong><small>自内核启动</small></article>
      <article><div className="metric-label"><span>成功率</span><Check size={17} /></div><strong>{monitor?.totals.success_rate ?? 100}<span>%</span></strong><small>{monitor?.totals.failures ?? 0} 次失败</small></article>
      <article><div className="metric-label"><span>Token</span><Zap size={17} /></div><strong>{formatNumber(monitor?.totals.tokens ?? 0)}</strong><small>累计消耗</small></article>
      <article><div className="metric-label"><span>在途请求</span><Clock3 size={17} /></div><strong>{monitor?.totals.in_flight ?? 0}</strong><small>当前执行中</small></article>
    </section>
    <section className="section-block"><div className="section-heading"><h2>插件健康</h2></div><div className="panel health-table"><div className="table-header"><span>插件</span><span>成功率</span><span>平均延迟</span><span>首字延迟</span><span>请求</span><span>在途</span></div>{plugins.map(plugin => { const metric = monitor?.plugins[plugin.name]; return <div className="health-row" key={plugin.name}><div className="plugin-name"><div className="plugin-icon"><Zap size={16} /></div><strong>{plugin.name.replace('2api-plugin-', '')}</strong></div><span className="numeric">{metric ? `${metric.success_rate}%` : '—'}</span><span className="numeric">{metric?.requests ? `${metric.average_latency_ms} ms` : '—'}</span><span className="numeric">{metric?.average_ttft_ms ? `${metric.average_ttft_ms} ms` : '—'}</span><span className="numeric">{metric?.requests ?? 0}</span><span className="numeric">{metric?.in_flight ?? 0}</span></div> })}</div></section>
    <section className="section-block"><div className="section-heading"><h2>事件流</h2><select className="event-filter" value={filter} onChange={event => setFilter(event.target.value)} aria-label="筛选事件"><option value="all">全部插件</option><option value="errors">仅异常</option>{plugins.map(plugin => <option key={plugin.name} value={plugin.name}>{plugin.name.replace('2api-plugin-', '')}</option>)}</select></div><div className="panel event-panel">{events.length ? events.map(event => <div className="event-row" key={event.id}><span className={`event-level ${event.level}`}>{event.level === 'error' ? '异常' : event.level === 'warning' ? '注意' : '信息'}</span><div className="event-main"><div><strong>{event.message}</strong><span className="event-plugin">{event.plugin.replace('2api-plugin-', '')}</span></div><span className="event-detail">{[event.model, event.account, event.duration_ms !== undefined ? `${event.duration_ms} ms` : '', event.tokens ? `${event.tokens} tokens` : ''].filter(Boolean).join(' · ')}</span></div><time>{new Date(event.timestamp * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time></div>) : <div className="event-empty">暂无事件</div>}</div></section>
  </>
}

function formatNumber(value: number) {
  return value >= 1_000_000 ? `${(value / 1_000_000).toFixed(2)}M` : value >= 1000 ? `${(value / 1000).toFixed(1)}K` : value.toLocaleString()
}

function PluginRow({ plugin, onDetails }: { plugin: Plugin; onDetails: (name: string) => void }) {
  const ready = plugin.status === 'running'
  const status = ready ? '运行中' : plugin.status === 'credential_missing' ? '待配置' : plugin.status === 'disabled' ? '已停用' : '异常'
  return <div className="plugin-row"><div className="plugin-name"><div className="plugin-icon"><Zap size={17} /></div><div><strong>{plugin.name.replace('2api-plugin-', '')}</strong><span>v{plugin.version}</span></div></div><span className={`status-label ${ready ? 'ready' : ''}`}><span className={`status-dot ${ready ? 'online' : ''}`} />{status}</span><span className="numeric">{plugin.models}</span><span className="numeric">{plugin.accounts.length}</span><button className="icon-button row-action" onClick={() => onDetails(plugin.name)} aria-label={`${plugin.name} 详情`}><ChevronRight size={16} /></button></div>
}

function WorkbuddyDetails({ plugin, apiKey, actionLocks, setActionLock }: { plugin: Plugin; apiKey: string; actionLocks: Record<string, AccountActionLock>; setActionLock: (uid: string, lock: AccountActionLock | null) => boolean }) {
  const [check, setCheck] = useState<'idle' | 'running' | 'success' | 'failed'>('idle')
  const pending = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; pending.current?.abort() }
  }, [])

  const checkConnection = async () => {
    if (pending.current) return
    const controller = new AbortController()
    pending.current = controller
    const timer = window.setTimeout(() => controller.abort(), 20000)
    setCheck('running')
    try {
      const response = await fetch(`/kernel/plugins/${encodeURIComponent(plugin.name)}/health/check`, {
        method: 'POST', headers: { Authorization: `Bearer ${apiKey}` }, signal: controller.signal,
      })
      const data = response.ok ? await response.json() : null
      if (controller.signal.aborted) throw new Error('timeout')
      if (mounted.current) setCheck(response.ok && data?.health?.ready === true ? 'success' : 'failed')
    } catch {
      if (mounted.current) setCheck('failed')
    } finally {
      window.clearTimeout(timer)
      pending.current = null
    }
  }

  return <>
    <p className="detail-copy">通过本机 WorkBuddy 网关调用模型。平台账号和额度请在原网关查看。</p>
    <p className="detail-copy">{!plugin.health?.last_checked ? '尚无自动巡检结果。' : plugin.health.ready ? `最近一次自动巡检通过：${new Date(plugin.health.last_checked * 1000).toLocaleString('zh-CN')}` : `最近一次自动巡检未通过：${new Date(plugin.health.last_checked * 1000).toLocaleString('zh-CN')}`}</p>
    <button className="secondary-button detail-action" onClick={() => void checkConnection()} disabled={check === 'running' || plugin.status !== 'running'}>{check === 'running' ? '正在测试…' : '测试本地网关连接'}</button>
    {check === 'success' && <p className="detail-result" role="status">连接测试通过，网关模型目录可访问。</p>}
    {check === 'failed' && <p className="detail-result failed" role="alert">连接测试未通过，请检查本地网关和访问密钥。</p>}
    <GatewayAccounts plugin={plugin} apiKey={apiKey} actionLocks={actionLocks} setActionLock={setActionLock} />
    <a className="secondary-button detail-link" href="http://127.0.0.1:7863/panel/operations/#accounts" target="_blank" rel="noopener noreferrer">打开 7863 WorkBuddy 账号面板<ArrowUpRight size={15} /></a>
  </>
}

function waitForPoll(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new Error('cancelled')); return }
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 500)
    const abort = () => { window.clearTimeout(timer); reject(new Error('cancelled')) }
    signal.addEventListener('abort', abort, { once: true })
  })
}

function GatewayAccounts({ plugin, apiKey, actionLocks, setActionLock }: { plugin: Plugin; apiKey: string; actionLocks: Record<string, AccountActionLock>; setActionLock: (uid: string, lock: AccountActionLock | null) => boolean }) {
  const [accountId, setAccountId] = useState(plugin.accounts[0]?.id ?? '')
  const [state, setState] = useState<'idle' | 'loading' | 'success' | 'failed'>('idle')
  const [result, setResult] = useState<GatewayAccountsResult | null>(null)
  const [resultAccountId, setResultAccountId] = useState('')
  const [storageError, setStorageError] = useState(false)
  const pending = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const selectedAccountId = plugin.accounts.some(account => account.id === accountId) ? accountId : plugin.accounts[0]?.id ?? ''
  const operationRunning = Object.values(actionLocks).includes('running')
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; pending.current?.abort() }
  }, [])

  const refresh = async () => {
    if (!selectedAccountId || pending.current) return
    const controller = new AbortController()
    pending.current = controller
    const timer = window.setTimeout(() => controller.abort(), 30000)
    setState('loading')
    setResult(null)
    setResultAccountId('')
    try {
      const headers = { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' }
      const started = await fetch(`/kernel/plugins/${encodeURIComponent(plugin.name)}/actions/gateway_accounts/runs`, {
        method: 'POST', headers, body: JSON.stringify({ accountId: selectedAccountId, input: {} }), signal: controller.signal,
      })
      if (!started.ok) throw new Error('start failed')
      const run = await started.json()
      if (controller.signal.aborted) throw new Error('timeout')
      if (typeof run.id !== 'string' || !run.id) throw new Error('invalid run')
      while (!controller.signal.aborted) {
        const response = await fetch(`/kernel/action-runs/${encodeURIComponent(run.id)}`, { headers, signal: controller.signal })
        if (!response.ok) throw new Error('read failed')
        const current = await response.json()
        if (controller.signal.aborted) throw new Error('timeout')
        if (current.state === 'succeeded') {
          const value = current.result
          if (value?.source !== 'gateway_cache' || !Array.isArray(value.accounts) || typeof value.truncated !== 'boolean') throw new Error('invalid result')
          if (mounted.current && !controller.signal.aborted) {
            setResult(value); setResultAccountId(selectedAccountId); setState('success')
            for (const [uid, lock] of Object.entries(actionLocks)) if (lock === 'succeeded' && !setActionLock(uid, null)) setStorageError(true)
          }
          return
        }
        if (current.state !== 'queued' && current.state !== 'running') throw new Error('run failed')
        await waitForPoll(controller.signal)
      }
      throw new Error('timeout')
    } catch {
      if (mounted.current) setState('failed')
    } finally {
      window.clearTimeout(timer)
      pending.current = null
    }
  }

  const setPaused = async (uid: string, paused: boolean) => {
    if (!selectedAccountId || pending.current || operationRunning || actionLocks[uid] || !/^[A-Za-z0-9_-]{1,64}$/.test(uid)) return
    if (!setActionLock(uid, 'running')) { setStorageError(true); return }
    const controller = new AbortController()
    pending.current = controller
    const timer = window.setTimeout(() => controller.abort(), 30000)
    try {
      const headers = { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' }
      const action = paused ? 'gateway_account_pause' : 'gateway_account_resume'
      const started = await fetch(`/kernel/plugins/${encodeURIComponent(plugin.name)}/actions/${action}/runs`, {
        method: 'POST', headers, body: JSON.stringify({ accountId: selectedAccountId, input: { uid }, idempotencyKey: crypto.randomUUID() }), signal: controller.signal,
      })
      if (!started.ok) throw new Error('start failed')
      const run = await started.json()
      if (controller.signal.aborted || typeof run.id !== 'string' || !run.id) throw new Error('invalid run')
      while (!controller.signal.aborted) {
        const response = await fetch(`/kernel/action-runs/${encodeURIComponent(run.id)}`, { headers, signal: controller.signal })
        if (!response.ok) throw new Error('read failed')
        const current = await response.json()
        if (controller.signal.aborted) throw new Error('timeout')
        if (current.state === 'succeeded') {
          if (!setActionLock(uid, 'succeeded') && mounted.current) setStorageError(true)
          return
        }
        if (current.state !== 'queued' && current.state !== 'running') throw new Error('unconfirmed')
        await waitForPoll(controller.signal)
      }
      throw new Error('timeout')
    } catch {
      if (!setActionLock(uid, 'unknown') && mounted.current) setStorageError(true)
    } finally {
      window.clearTimeout(timer)
      pending.current = null
    }
  }

  return <section className="gateway-accounts">
    <strong>原网关账号缓存</strong>
    <p>只读缓存，更新时间未知；积分为 0 也可能代表未知。账号标识与缓存积分会记录到本地动作历史。</p>
    <p>暂停或恢复仅影响原网关选号；保活等后台任务仍运行。为防止误操作，账号标识和结果锁定状态会保存在本次浏览器会话。</p>
    {plugin.accounts.length > 1 && <label>使用纳川桥接账号<select value={selectedAccountId} onChange={event => { setAccountId(event.target.value); setState('idle'); setResult(null) }} disabled={state === 'loading' || operationRunning}>{plugin.accounts.map(account => <option key={account.id} value={account.id}>{account.id}</option>)}</select></label>}
    <button className="secondary-button detail-action" onClick={() => void refresh()} disabled={!selectedAccountId || state === 'loading' || operationRunning || plugin.status !== 'running'}>{state === 'loading' ? '正在读取…' : '读取原网关账号缓存'}</button>
    {state === 'failed' && <p className="detail-result failed" role="alert">读取失败或超时，请检查桥接账号和原网关。</p>}
    {storageError && <p className="detail-result failed" role="alert">浏览器无法保存操作锁定状态，已停止操作；请检查浏览器设置。</p>}
    {state === 'success' && result && resultAccountId === selectedAccountId && <div className="gateway-account-list" role="region" aria-label="原网关账号缓存">
      {result.truncated && <p>仅显示前 1000 个账号。</p>}
      {!result.accounts.length && <p>原网关没有返回账号。</p>}
      {result.accounts.map((account, index) => {
        const flags = [account.disabled && '停用', account.paused && '暂停', account.cooling && '冷却'].filter(Boolean)
        const knownFlags = [account.disabled, account.paused, account.cooling].every(value => typeof value === 'boolean')
        const credits = typeof account.cachedCredits === 'number' && Number.isFinite(account.cachedCredits) && account.cachedCredits >= 0 ? account.cachedCredits : null
        const total = typeof account.cachedCreditsTotal === 'number' && Number.isFinite(account.cachedCreditsTotal) && account.cachedCreditsTotal > 0 ? account.cachedCreditsTotal : null
        const eligible = /^[A-Za-z0-9_-]{1,64}$/.test(account.uid)
        const lock = actionLocks[account.uid]
        return <article key={`${account.uid}-${index}`}><strong>{account.nickname || account.uid}</strong><span>{account.nickname ? `${account.uid} · ` : ''}{account.realm || '地域未知'}</span><span>缓存积分：{credits === null ? '未知' : credits === 0 ? '0（可能未知）' : credits.toLocaleString()}{total === null ? '' : ` / ${total.toLocaleString()}`}</span><span>{flags.length ? flags.join('、') : knownFlags ? '无停用、暂停或冷却标记' : '状态信息不完整'}</span>{eligible ? <>{typeof account.paused !== 'boolean' && <span>暂停状态未知，请自行选择操作。</span>}{lock === 'running' && <span role="status">操作进行中，请勿重复提交。</span>}{lock === 'succeeded' && <span role="status">操作已完成，请重新读取缓存后再操作。</span>}{lock === 'unknown' && <><span role="alert">结果无法确认，请先到原面板核对。</span><button className="secondary-button gateway-account-action" onClick={() => { if (setActionLock(account.uid, null)) { setState('idle'); setResult(null) } else setStorageError(true) }}>已在原面板核对</button></>}<div className="gateway-account-actions">{account.paused !== true && <button className="secondary-button gateway-account-action" aria-label={`暂停 ${account.uid} 选号`} disabled={!!lock || operationRunning || storageError} onClick={() => void setPaused(account.uid, true)}>暂停选号</button>}{account.paused !== false && <button className="secondary-button gateway-account-action" aria-label={`恢复 ${account.uid} 选号`} disabled={!!lock || operationRunning || storageError} onClick={() => void setPaused(account.uid, false)}>恢复选号</button>}</div></> : <span>账号标识不支持操作</span>}</article>
      })}
    </div>}
  </section>
}

function SearchField({ value, onChange, placeholder }: { value: string; onChange: (value: string) => void; placeholder: string }) {
  return <div className="search-field"><Search size={16} /><input value={value} onChange={event => onChange(event.target.value)} placeholder={placeholder} />{value && <button onClick={() => onChange('')} aria-label="清除搜索"><X size={14} /></button>}</div>
}

function EmptyState({ icon: Icon, title, text, action }: { icon: typeof Box; title: string; text: string; action?: React.ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><Icon size={24} strokeWidth={1.5} /></div><h3>{title}</h3><p>{text}</p>{action}</div>
}

function Modal({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [onClose])
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}><section className="modal" role="dialog" aria-modal="true"><button className="icon-button modal-close" onClick={onClose} aria-label="关闭"><X size={18} /></button>{children}</section></div>
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
