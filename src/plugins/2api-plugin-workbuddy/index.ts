import { openAIBusiness } from '../../plugin-runtime/dist/openai.js'
import { classifyHTTPStatus } from '../../plugin-runtime/dist/contracts.js'
import type { ActionContext, PluginFactory } from '../../plugin-runtime/dist/contracts.js'

const models = [{ id: 'glm-5.2' }, { id: 'glm-5.3' }, { id: 'deepseek-v4.1-flash' }]

// Credit is measured by the original gateway. Missing or malformed data stays unknown.
function mapUsage(message: Record<string, any>): Record<string, any> {
  if (!message.usage || typeof message.usage !== 'object' || Array.isArray(message.usage)) return message
  const { vendor: _vendor, ...usage } = message.usage
  const credit = usage.credit
  if (typeof credit === 'number' && Number.isFinite(credit) && credit >= 0) {
    usage.vendor = { cost: credit, unit: 'workbuddy_credit', dimension: 'chat' }
  }
  return { ...message, usage }
}

const factory: PluginFactory = context => {
  const base = openAIBusiness(models)(context)
  const baseUrl = String(context.config.baseUrl ?? '').replace(/\/$/, '')
  async function setAccountPaused({ credential, input }: ActionContext, signal: AbortSignal, operation: 'pause' | 'resume') {
    const key = credential?.secret.apiKey ?? credential?.secret.accessToken
    if (typeof key !== 'string' || !key) throw new Error('网关密钥缺失')
    const uid = input.uid
    // Match the original gateway's UID validation and never put arbitrary input in a URL.
    if (typeof uid !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(uid)) throw new Error('原网关账号标识无效')
    try {
      const response = await fetch(`${baseUrl}/panel/api/accounts/${encodeURIComponent(uid)}/${operation}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${key}` },
        signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]),
        redirect: 'error',
      })
      if (!response.ok) throw new Error('gateway error')
      const result = await response.json() as { ok?: unknown }
      if (result?.ok !== true) throw new Error('unconfirmed')
      return { ok: true }
    } catch {
      // Never expose raw upstream responses, malformed JSON, or network errors.
      throw new Error('原网关账号操作未确认')
    }
  }
  return {
    ...base,
    healthCheck: async credential => {
      const key = credential.secret.apiKey ?? credential.secret.accessToken
      if (typeof key !== 'string' || !key) throw new Error('网关密钥缺失')
      const response = await fetch(baseUrl + '/v1/models', {
        headers: { Authorization: `Bearer ${key}` },
        signal: AbortSignal.timeout(10000),
        redirect: 'error',
      })
      if (!response.ok) classifyHTTPStatus(response.status)
      const body = await response.json() as { data?: unknown }
      const ids = new Set(Array.isArray(body.data) ? body.data.map((m: any) => m?.id) : [])
      const modelsReady = models.some(model => ids.has(`cn:${model.id}`))
      return { reachable: true, authenticated: true, modelsReady, accountReady: true }
    },
    completion: async (params, signal) => mapUsage(await base.completion(params, signal)),
    stream: async function* (params, signal) {
      for await (const chunk of base.stream(params, signal)) {
        yield JSON.stringify(mapUsage(JSON.parse(chunk)))
      }
    },
    actions: {
      gateway_account_pause: (context, signal) => setAccountPaused(context, signal, 'pause'),
      gateway_account_resume: (context, signal) => setAccountPaused(context, signal, 'resume'),
      gateway_accounts: async ({ credential }, signal) => {
        const key = credential?.secret.apiKey ?? credential?.secret.accessToken
        if (typeof key !== 'string' || !key) throw new Error('网关密钥缺失')
        const response = await fetch(baseUrl + '/status', {
          headers: { Authorization: `Bearer ${key}` },
          signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]),
          redirect: 'error',
        })
        if (!response.ok) throw new Error('网关状态查询失败')
        const body = await response.json() as { accounts?: unknown }
        if (!Array.isArray(body.accounts)) throw new Error('网关状态格式错误')
        const accounts = body.accounts.slice(0, 1000).map((item: unknown) => {
          if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('网关账号状态格式错误')
          const account = item as Record<string, unknown>
          if (typeof account.uid !== 'string' || !account.uid || account.uid.length > 256) throw new Error('网关账号标识格式错误')
          const value = (x: unknown) => typeof x === 'number' && Number.isSafeInteger(x) && x >= 0 ? x : undefined
          const cachedCredits = value(account.credits)
          // The original gateway uses 0 to mean unknown for total credit.
          const cachedCreditsTotal = value(account.credits_total)
          return {
            uid: account.uid,
            nickname: typeof account.nickname === 'string' ? account.nickname.slice(0, 256) : '',
            realm: typeof account.realm === 'string' ? account.realm.slice(0, 32) : '',
            ...(cachedCredits !== undefined ? { cachedCredits } : {}),
            ...(cachedCreditsTotal !== undefined && cachedCreditsTotal > 0 ? { cachedCreditsTotal } : {}),
            ...(typeof account.disabled === 'boolean' ? { disabled: account.disabled } : {}),
            ...(typeof account.paused === 'boolean' ? { paused: account.paused } : {}),
            ...(typeof account.cooling === 'boolean' ? { cooling: account.cooling } : {}),
          }
        })
        return { source: 'gateway_cache', accounts, truncated: body.accounts.length > accounts.length }
      },
    },
  }
}
export default factory
