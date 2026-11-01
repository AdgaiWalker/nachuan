import { useEffect, useRef, useState } from 'react'

type Model = { id: string; owned_by: string }
type Props = { apiKey: string; models: Model[] }
type Phase = 'idle' | 'running' | 'done' | 'cancelled' | 'error'

/** Incrementally decode SSE events, including CRLF, split UTF-8 and multiline data. */
export class SseDecoder {
  private decoder = new TextDecoder()
  private buffer = ''
  private lines: string[] = []

  push(chunk: Uint8Array, end = false): string[] {
    this.buffer += this.decoder.decode(chunk, { stream: !end })
    const events: string[] = []
    let boundary: number
    while ((boundary = this.buffer.search(/[\r\n]/)) !== -1) {
      if (this.buffer[boundary] === '\r' && boundary === this.buffer.length - 1 && !end) break
      const line = this.buffer.slice(0, boundary)
      const width = this.buffer[boundary] === '\r' && this.buffer[boundary + 1] === '\n' ? 2 : 1
      this.buffer = this.buffer.slice(boundary + width)
      this.accept(line, events)
    }
    if (this.buffer.length > 2 * 1024 * 1024 || this.lines.join('').length > 2 * 1024 * 1024) throw new Error('响应数据过大')
    if (end) {
      if (this.buffer) this.accept(this.buffer, events)
      this.buffer = ''
      this.accept('', events)
    }
    return events
  }

  private accept(line: string, events: string[]) {
    if (!line) {
      if (this.lines.length) events.push(this.lines.join('\n'))
      this.lines = []
    } else if (line === 'data') {
      this.lines.push('')
    } else if (line.startsWith('data:')) {
      this.lines.push(line.slice(5).replace(/^ /, ''))
    }
  }
}

export function WorkbuddyPlayground({ apiKey, models }: Props) {
  const available = models.filter(model => model.owned_by === '2api-plugin-workbuddy' && model.id.startsWith('workbuddy:'))
  const [selected, setSelected] = useState('')
  const [prompt, setPrompt] = useState('')
  const [content, setContent] = useState('')
  const [reasoning, setReasoning] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [message, setMessage] = useState('')
  const [toolCalls, setToolCalls] = useState(false)
  const active = useRef<AbortController | null>(null)
  const mounted = useRef(false)
  const model = available.some(item => item.id === selected) ? selected : available[0]?.id ?? ''

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      active.current?.abort()
      active.current = null
    }
  }, [])

  function cancel() {
    const controller = active.current
    if (!controller) return
    active.current = null
    controller.abort()
    setPhase('cancelled')
    setMessage('请求已取消；已生成的部分保留。')
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!model || !prompt.trim() || !apiKey || active.current) return
    const controller = new AbortController()
    active.current = controller
    setContent('')
    setReasoning('')
    setToolCalls(false)
    setMessage('')
    setPhase('running')
    let receivedText = false
    let receivedTool = false
    let done = false
    const current = () => mounted.current && active.current === controller
    try {
      const response = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ model, messages: [{ role: 'user', content: prompt.trim() }], stream: true, max_tokens: 256 }),
        signal: controller.signal,
      })
      if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`)
      if (!response.body) throw new Error('未收到流式响应')
      const reader = response.body.getReader()
      const parser = new SseDecoder()
      try {
        while (!done && current()) {
          const next = await reader.read()
          const events = parser.push(next.value ?? new Uint8Array(), next.done)
          for (const raw of events) {
            if (raw.trim() === '[DONE]') { done = true; break }
            let data: any
            try { data = JSON.parse(raw) } catch { throw new Error('流式响应格式错误') }
            if (data?.error) throw new Error('上游返回流式错误')
            const delta = Array.isArray(data?.choices) ? data.choices[0]?.delta : undefined
            if (typeof delta?.content === 'string' && delta.content) {
              receivedText = true
              if (current()) setContent(value => value + delta.content)
            }
            if (typeof delta?.reasoning_content === 'string' && delta.reasoning_content) {
              receivedText = true
              if (current()) setReasoning(value => value + delta.reasoning_content)
            }
            if (Array.isArray(delta?.tool_calls) && delta.tool_calls.length) {
              receivedTool = true
              if (current()) setToolCalls(true)
            }
          }
          if (next.done && !done) throw new Error('流式响应意外中断，未收到结束标记')
        }
      } finally {
        // Stop the upstream even after malformed frames or an SSE error.
        await reader.cancel().catch(() => undefined)
        reader.releaseLock()
      }
      if (current()) {
        setPhase('done')
        setMessage(receivedText ? '回复完成。' : receivedTool ? '响应结束；收到工具调用但本页面不执行工具。' : '响应结束，未收到可显示的文本。')
      }
    } catch (error) {
      if (current()) {
        setPhase('error')
        setMessage(error instanceof Error ? error.message : '请求失败')
      }
    } finally {
      if (active.current === controller) active.current = null
    }
  }

  return <section className="section-block" aria-labelledby="workbuddy-playground-heading">
    <div className="section-heading"><h2 id="workbuddy-playground-heading">WorkBuddy 调试</h2></div>
    <div className="panel" style={{ padding: 20 }}>
      <p style={{ margin: '0 0 16px', fontSize: 12, color: '#6e6e73', lineHeight: 1.6 }}>请求会通过已连接的本地 WorkBuddy 网关发送，可能消耗账号积分；每次最多请求 256 个输出 Token。</p>
      {available.length === 0 ? <p>未发现可用的 WorkBuddy 模型，请先配置本地网关账号并加载插件。</p> : <form onSubmit={submit}>
        <label style={{ display: 'block', fontSize: 12, marginBottom: 8 }}>模型
          <select aria-label="模型" value={model} onChange={event => setSelected(event.target.value)} disabled={phase === 'running'} style={{ display: 'block', width: '100%', marginTop: 6, padding: 9 }}>
            {available.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}
          </select>
        </label>
        <label style={{ display: 'block', fontSize: 12, marginBottom: 12 }}>消息
          <textarea aria-label="消息" value={prompt} onChange={event => setPrompt(event.target.value)} disabled={phase === 'running'} maxLength={5000} rows={4} style={{ display: 'block', width: '100%', marginTop: 6, padding: 9, resize: 'vertical' }} />
        </label>
        {phase === 'running' ? <button className="secondary-button" type="button" onClick={cancel}>取消请求</button> : <button className="primary-button" type="submit" disabled={!apiKey || !prompt.trim()}>发送</button>}
      </form>}
      {message && <p role={phase === 'error' ? 'alert' : 'status'} style={{ fontSize: 12, color: phase === 'error' ? '#b25d44' : '#6e6e73' }}>{message}</p>}
      {reasoning && <details><summary>思考内容</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{reasoning}</pre></details>}
      {content && <div aria-label="回复内容" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginTop: 16, fontSize: 13 }}>{content}</div>}
      {toolCalls && <p style={{ fontSize: 12 }}>此响应包含工具调用，本页面不会执行工具。</p>}
    </div>
  </section>
}
