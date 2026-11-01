export const PROTOCOL_VERSION = 1 as const

export interface ModelContract {
  id: string
  upstreamId?: string
  maxTokens?: number
  capabilities?: string[]
  optionsSchema?: Record<string, any>
}

export interface PluginInit {
  spec: { name: string; version: string; specVersion?: number; credentialSchema?: Record<string, any>; actions?: ActionContract[]; quota?: Record<string, any> }
  config: Record<string, unknown>
}

export interface CredentialContext {
  id: string
  secret: Record<string, unknown>
}

export type FailureKind = 'rate_limit' | 'reauth' | 'exhausted' | 'transient' | 'invalid_request' | 'unknown'

export class BusinessError extends Error {
  constructor(public kind: FailureKind, message: string, public retryAfter = 0) { super(message) }
}

export function classifyHTTPStatus(status: number): never {
  if (status === 429) throw new BusinessError('rate_limit', `上游返回 HTTP ${status}`, 60)
  if (status === 401 || status === 403) throw new BusinessError('reauth', `上游返回 HTTP ${status}`)
  if (status === 402) throw new BusinessError('exhausted', `上游返回 HTTP ${status}`)
  if (status === 408 || status === 409 || status === 425 || status >= 500) throw new BusinessError('transient', `上游返回 HTTP ${status}`)
  if (status >= 400) throw new BusinessError('invalid_request', `上游返回 HTTP ${status}`)
  throw new BusinessError('unknown', `上游返回 HTTP ${status}`)
}

export interface ChatParams {
  credential: CredentialContext
  request: {
    model: string
    messages: unknown[]
    stream?: boolean
    [key: string]: unknown
  }
  model: string
}

export interface OpenAIChunk {
  id: string
  object: 'chat.completion.chunk'
  created: number
  model: string
  choices: Array<{
    index: number
    delta: { role?: 'assistant'; content?: string; reasoning_content?: string; tool_calls?: unknown[] }
    finish_reason: string | null
  }>
  usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number }
}

export interface ActionContract {
  id: string
  scope?: 'plugin' | 'account'
  inputSchema?: Record<string, any>
  outputSchema?: Record<string, any>
  mutatesCredentials?: boolean
}
export interface ActionContext {
  credential?: CredentialContext
  input: Record<string, unknown>
}
export interface BusinessPlugin {
  init(context: PluginInit): Promise<{ models: ModelContract[] }> | { models: ModelContract[] }
  completion(params: ChatParams, signal?: AbortSignal): Promise<Record<string, unknown>>
  stream(params: ChatParams, signal?: AbortSignal): AsyncIterable<string>
  executeTask?(action: string): Promise<Record<string, unknown>>
  healthCheck?(credential: CredentialContext): Promise<{ reachable: boolean; authenticated: boolean; modelsReady: boolean; accountReady: boolean }>
  refreshCredential?(credential: CredentialContext): Promise<Record<string, unknown>>
  actions?: Record<string, (context: ActionContext, signal: AbortSignal) => Promise<Record<string, unknown>>>
  validateCredential?(credential: Record<string, unknown>, signal: AbortSignal): Promise<{valid: boolean}>
  queryQuota?(credential: CredentialContext, signal: AbortSignal): Promise<Record<string, unknown>>
  close?(): Promise<void>
}

export type PluginFactory = (context: PluginInit) => BusinessPlugin | Promise<BusinessPlugin>
