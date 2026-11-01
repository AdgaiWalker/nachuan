import type { BusinessPlugin, ChatParams, ModelContract, PluginInit } from './contracts.js'
import { BusinessError, classifyHTTPStatus } from './contracts.js'
import { normalizeChunk, sseData } from './normalize.js'

/** 通用 OpenAI 兼容出站适配；平台特有的转换由各自业务插件提供。 */
export function openAIBusiness(models: ModelContract[]) {
  return (context: PluginInit): BusinessPlugin => {
    const config = context.config
    const baseUrl = String(config.baseUrl ?? '').replace(/\/$/, '')
    const chatPath = String(config.chatPath ?? '/v1/chat/completions')
    const headers = (config.headers && typeof config.headers === 'object' ? config.headers : {}) as Record<string, string>

    async function upstream(params: ChatParams, stream: boolean, signal?: AbortSignal): Promise<Response> {
      if (!baseUrl) throw new Error('业务插件缺少 config.baseUrl')
      const requestHeaders: Record<string, string> = { ...headers, 'Content-Type': 'application/json', Accept: stream ? 'text/event-stream' : 'application/json' }
      const apiKey = String(params.credential.secret.apiKey ?? params.credential.secret.accessToken ?? config.apiKey ?? '')
      if (apiKey) requestHeaders.Authorization = `Bearer ${apiKey}`
      const response = await fetch(baseUrl + chatPath, {
        method: 'POST', headers: requestHeaders,
        body: JSON.stringify({ ...params.request, model: params.model, stream }),
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(Number(config.timeoutSeconds ?? 120) * 1000)]) : AbortSignal.timeout(Number(config.timeoutSeconds ?? 120) * 1000),
      })
      if (!response.ok) classifyHTTPStatus(response.status)
      return response
    }

    return {
      init: () => ({ models }),
      healthCheck: async credential => {
        const response = await fetch(baseUrl + String(config.modelsPath ?? '/v1/models'), { headers: { Authorization: `Bearer ${String(credential.secret.apiKey ?? credential.secret.accessToken ?? '')}` }, signal: AbortSignal.timeout(10000) })
        if (!response.ok) classifyHTTPStatus(response.status)
        const body = await response.json() as { data?: unknown[] }
        const modelsReady = Array.isArray(body.data) && body.data.length > 0
        return { reachable: true, authenticated: true, modelsReady, accountReady: true }
      },
      completion: async (params, signal) => await (await upstream(params, false, signal)).json() as Record<string, unknown>,
      stream: async function* (params, signal) {
        const response = await upstream(params, true, signal)
        for await (const data of sseData(response)) {
          if (data.trim() === '[DONE]') return
          const payload = JSON.parse(data)
          if (payload?.error) {
            const error = payload.error
            const code = String(error.code ?? error.type ?? '')
            const kind = code.includes('quota') ? 'exhausted'
              : code.includes('rate_limit') ? 'rate_limit'
              : code.includes('auth') || code.includes('api_key') ? 'reauth'
              : code.includes('invalid') ? 'invalid_request' : 'transient'
            throw new BusinessError(kind, String(error.message ?? '上游流式错误'), kind === 'exhausted' ? 0 : 60)
          }
          yield JSON.stringify(normalizeChunk(payload, params.model))
        }
        // A clean TCP EOF is not proof that an OpenAI-compatible stream finished.
        // Surface truncation instead of letting the host synthesize a successful [DONE].
        throw new BusinessError('transient', '上游流意外结束，未收到 [DONE] 标记')
      },
    }
  }
}
