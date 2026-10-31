import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
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
}
type Model = { id: string; owned_by: string; max_output_tokens?: number }
type MonitorEvent = { id: string; timestamp: number; level: string; plugin: string; message: string; model?: string; duration_ms?: number; tokens?: number; account?: string }
type MonitorData = { uptime_seconds: number; totals: { requests?: number; successes?: number; failures?: number; in_flight?: number; tokens?: number; success_rate: number }; plugins: Record<string, { requests: number; successes: number; failures: number; in_flight: number; tokens: number; average_latency_ms: number; average_ttft_ms: number; success_rate: number }>; events: MonitorEvent[] }
type UsageRecord = { request_id: string; timestamp: number; plugin: string; model: string; account: string; success: boolean; duration_ms: number; total_tokens: number; prompt_tokens: number; completion_tokens: number; usage_source: string; vendor_cost?: number; vendor_unit?: string; vendor_remaining?: number }
type LedgerData = { totals: { requests?: number; prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; vendor_cost?: number; success_rate: number }; records: UsageRecord[] }
type View = 'overview' | 'accounts' | 'models' | 'monitor' | 'usage' | 'settings'

const nav = [
  { id: 'overview' as View, label: '概览', icon: LayoutDashboard },
  { id: 'accounts' as View, label: '账号与凭证', icon: UsersRound },
  { id: 'models' as View, label: '模型', icon: Box },
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

  const load = useCallback(async (key: string) => {
    setLoading(true)
    setError('')
    try {
      const headers = { Authorization: `Bearer ${key}` }
      const [pluginsResponse, modelsResponse, monitorResponse, ledgerResponse] = await Promise.all([
        fetch('/kernel/plugins', { headers }),
        fetch('/v1/models', { headers }),
        fetch('/kernel/monitor', { headers }),
        fetch('/kernel/ledger', { headers }),
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
      if (ledgerResponse.ok) setLedger(await ledgerResponse.json())
      setConnected(true)
    } catch {
      setError('无法连接纳川内核')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load(apiKey) }, [apiKey, load])

  useEffect(() => {
    if (!connected || view !== 'usage') return
    void fetch(`/kernel/ledger?plugin=${encodeURIComponent(usagePlugin === 'all' ? '' : usagePlugin)}`, { headers: { Authorization: `Bearer ${apiKey}` } }).then(async response => { if (response.ok) setLedger(await response.json()) })
  }, [apiKey, connected, view, usagePlugin])

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
      if (!response.ok) throw new Error(result.error || '添加失败')
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
          <section className="section-block"><div className="section-heading"><h2>插件</h2><button className="text-button" onClick={() => navigate('accounts')}>查看全部<ChevronRight size={14} /></button></div><div className="panel plugin-panel"><div className="table-header"><span>插件</span><span>状态</span><span>模型</span><span>账号</span><span /></div>{filteredPlugins.map(plugin => <PluginRow key={plugin.name} plugin={plugin} />)}</div></section>
          <section className="section-block"><div className="section-heading"><h2>快捷操作</h2></div><div className="quick-grid"><button onClick={() => { setSelectedPlugin(''); setFormError(''); setAddOpen(true) }}><span className="quick-icon"><Plus size={19} /></span><span><strong>添加账号</strong><small>接入新的上游凭证</small></span><ChevronRight size={16} /></button><button onClick={() => navigate('models')}><span className="quick-icon"><Cpu size={19} /></span><span><strong>浏览模型</strong><small>查看已聚合的模型</small></span><ChevronRight size={16} /></button><button onClick={() => navigate('settings')}><span className="quick-icon"><KeyRound size={19} /></span><span><strong>接入信息</strong><small>API 地址与访问密钥</small></span><ChevronRight size={16} /></button></div></section>
        </>}
        {view === 'accounts' && <>
          <section className="heading"><div><h1>账号与凭证</h1><p>管理插件的上游账号。</p></div><button className="primary-button" onClick={() => { setSelectedPlugin(''); setFormError(''); setAddOpen(true) }}><Plus size={16} />添加账号</button></section>
          <SearchField value={search} onChange={setSearch} placeholder="搜索插件" />
          <div className="panel plugin-panel"><div className="table-header"><span>插件</span><span>状态</span><span>模型</span><span>账号</span><span /></div>{filteredPlugins.map(plugin => <PluginRow key={plugin.name} plugin={plugin} />)}</div>
        </>}
        {view === 'models' && <>
          <section className="heading"><div><h1>模型</h1><p>来自已就绪插件的统一模型目录。</p></div></section>
          <SearchField value={search} onChange={setSearch} placeholder="搜索模型" />
          {filteredModels.length ? <div className="model-grid">{filteredModels.map(model => <article className="model-card" key={model.id}><div className="model-symbol"><Box size={20} /></div><strong>{model.id}</strong><span>{model.owned_by}</span><div className="model-card-bottom"><span>{model.max_output_tokens ? `${(model.max_output_tokens / 1024).toLocaleString()}K` : 'OpenAI'}</span><ArrowUpRight size={16} /></div></article>)}</div> : <EmptyState icon={Box} title={search ? '没有匹配的模型' : '还没有可用模型'} text={search ? '尝试其他关键词' : '添加账号后，模型会显示在这里'} action={!search && <button className="secondary-button" onClick={() => { setFormError(''); setAddOpen(true) }}><Plus size={15} />添加账号</button>} />}
        </>}
        {view === 'usage' && <UsageView ledger={ledger} plugins={plugins} filter={usagePlugin} setFilter={setUsagePlugin} />}
        {view === 'monitor' && <MonitorView monitor={monitor} plugins={plugins} filter={eventFilter} setFilter={setEventFilter} />}
        {view === 'settings' && <>
          <section className="heading"><div><h1>设置</h1><p>本地网关接入信息。</p></div></section>
          <section className="settings-panel panel"><div className="setting-row"><div><strong>API 地址</strong><span>OpenAI 兼容接口</span></div><div className="copy-field"><code>http://127.0.0.1:7865/v1</code><button className="icon-button" aria-label="复制 API 地址" onClick={async () => { await navigator.clipboard.writeText('http://127.0.0.1:7865/v1'); setCopied(true); setTimeout(() => setCopied(false), 1800) }}>{copied ? <Check size={16} /> : <Copy size={16} />}</button></div></div><div className="setting-row"><div><strong>访问密钥</strong><span>保存在当前浏览器会话</span></div><button className="secondary-button" onClick={() => { setKeyDraft(''); setAuthOpen(true) }}><KeyRound size={15} />{apiKey ? '更换密钥' : '设置密钥'}</button></div><div className="setting-row"><div><strong>内核</strong><span>Python 宿主 · TypeScript 业务插件</span></div><span className="subtle-badge"><ShieldCheck size={14} />本地运行</span></div></section>
        </>}
      </div>
    </main>

    {authOpen && <Modal onClose={() => setAuthOpen(false)}><div className="modal-icon"><KeyRound size={22} /></div><h2>连接纳川</h2><p>输入 API 访问密钥。</p><form onSubmit={saveKey}><label>访问密钥<input autoFocus type="password" value={keyDraft} onChange={event => setKeyDraft(event.target.value)} placeholder="API Key" required /></label><button className="primary-button full" type="submit">连接</button></form></Modal>}
    {addOpen && <Modal onClose={() => setAddOpen(false)}><div className="modal-icon"><Plus size={22} /></div><h2>添加账号</h2><p>凭证仅保存在对应插件目录。</p>{formError && <div className="form-error">{formError}</div>}<form onSubmit={addAccount}><label>插件<select value={selectedPlugin} onChange={event => setSelectedPlugin(event.target.value)} required><option value="" disabled>选择插件</option>{plugins.map(plugin => <option key={plugin.name} value={plugin.name}>{plugin.name}</option>)}</select></label><label>账号名称<input value={accountId} onChange={event => setAccountId(event.target.value)} placeholder="账号标识" required pattern="[A-Za-z0-9._-]+" /></label><label>上游 API Key<input type="password" value={credential} onChange={event => setCredential(event.target.value)} placeholder="sk-••••••••••••" required /></label><button className="primary-button full" type="submit" disabled={adding}>{adding ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}添加账号</button></form></Modal>}
  </div>
}

function UsageView({ ledger, plugins, filter, setFilter }: { ledger: LedgerData | null; plugins: Plugin[]; filter: string; setFilter: (value: string) => void }) {
  return <>
    <section className="heading"><div><h1>用量</h1><p>Token 与上游结算记录。</p></div><select className="event-filter" value={filter} onChange={event => setFilter(event.target.value)} aria-label="按插件筛选用量"><option value="all">全部插件</option>{plugins.map(plugin => <option key={plugin.name} value={plugin.name}>{plugin.name.replace('2api-plugin-', '')}</option>)}</select></section>
    <section className="metrics">
      <article><div className="metric-label"><span>输入 Token</span><ArrowUpRight size={17} /></div><strong>{formatNumber(ledger?.totals.prompt_tokens ?? 0)}</strong><small>上游报告</small></article>
      <article><div className="metric-label"><span>输出 Token</span><Zap size={17} /></div><strong>{formatNumber(ledger?.totals.completion_tokens ?? 0)}</strong><small>上游报告</small></article>
      <article><div className="metric-label"><span>总 Token</span><ReceiptText size={17} /></div><strong>{formatNumber(ledger?.totals.total_tokens ?? 0)}</strong><small>{ledger?.totals.requests ?? 0} 次请求</small></article>
      <article><div className="metric-label"><span>平台结算</span><ReceiptText size={17} /></div><strong>{ledger?.totals.vendor_cost?.toLocaleString() ?? '—'}</strong><small>仅实际上报</small></article>
    </section>
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

function PluginRow({ plugin }: { plugin: Plugin }) {
  const ready = plugin.status === 'running'
  const status = ready ? '运行中' : plugin.status === 'credential_missing' ? '待配置' : plugin.status === 'disabled' ? '已停用' : '异常'
  return <div className="plugin-row"><div className="plugin-name"><div className="plugin-icon"><Zap size={17} /></div><div><strong>{plugin.name.replace('2api-plugin-', '')}</strong><span>v{plugin.version}</span></div></div><span className={`status-label ${ready ? 'ready' : ''}`}><span className={`status-dot ${ready ? 'online' : ''}`} />{status}</span><span className="numeric">{plugin.models}</span><span className="numeric">{plugin.accounts.length}</span><button className="icon-button row-action" aria-label={`${plugin.name} 详情`}><ChevronRight size={16} /></button></div>
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
