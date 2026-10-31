import { createInterface } from 'node:readline'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import type { BusinessPlugin, PluginFactory, PluginInit, ChatParams } from './contracts.js'
import { BusinessError } from './contracts.js'
import { PROTOCOL_VERSION } from './contracts.js'

interface Request { jsonrpc: '2.0'; id: number; method: string; params: any }

function send(message: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify(message) + '\n')
}

async function main(): Promise<void> {
  const entry = process.argv[2]
  if (!entry) throw new Error('plugin entry required')
  const imported = await import(pathToFileURL(resolve(entry)).href) as { default: PluginFactory }
  if (typeof imported.default !== 'function') throw new Error('插件必须 default export PluginFactory')
  let plugin: BusinessPlugin | undefined
  const reader = createInterface({ input: process.stdin, crlfDelay: Infinity })
  const running = new Set<Promise<void>>()
  const cancellations = new Map<number, AbortController>()
  for await (const line of reader) {
    let request: Request
    try { request = JSON.parse(line) } catch { continue }
    if (request.method === 'request.cancel') {
      cancellations.get(request.params.id)?.abort()
      cancellations.delete(request.params.id)
      continue
    }
    const controller = new AbortController()
    const task = dispatch(request, controller.signal).catch(error => {
      send({ jsonrpc: '2.0', id: request.id, error: { code: -32000, message: error instanceof Error ? error.message : String(error), data: error instanceof BusinessError ? { kind: error.kind, retryAfter: error.retryAfter } : undefined } })
    })
    running.add(task)
    cancellations.set(request.id, controller)
    void task.finally(() => { running.delete(task); cancellations.delete(request.id) })
  }
  for (const controller of cancellations.values()) controller.abort()
  await Promise.all(running)
  await plugin?.close?.()

  async function dispatch(request: Request, signal: AbortSignal): Promise<void> {
    if (request.method === 'plugin.init') {
      const init = request.params as PluginInit
      plugin = await imported.default(init)
      const registered = await plugin.init(init)
      send({ jsonrpc: '2.0', id: request.id, result: { protocol: PROTOCOL_VERSION, models: registered.models } })
      return
    }
    if (!plugin) throw new Error('plugin not initialized')
    if (request.method === 'task.execute') {
      if (!plugin.executeTask) throw new Error('插件不支持周期任务')
      send({ jsonrpc: '2.0', id: request.id, result: await plugin.executeTask(request.params.action) })
      return
    }
    if (request.method === 'credential.health') {
      if (!plugin.healthCheck) throw new Error('插件不支持健康探测')
      send({ jsonrpc: '2.0', id: request.id, result: await plugin.healthCheck(request.params.credential) })
      return
    }
    if (request.method === 'credential.refresh') {
      if (!plugin.refreshCredential) throw new Error('插件不支持凭证刷新')
      send({ jsonrpc: '2.0', id: request.id, result: await plugin.refreshCredential(request.params.credential) })
      return
    }
    if (request.method === 'chat.completion') {
      send({ jsonrpc: '2.0', id: request.id, result: await plugin.completion(request.params as ChatParams, signal) })
      return
    }
    if (request.method === 'chat.stream') {
      try {
        for await (const chunk of plugin.stream(request.params as ChatParams, signal)) send({ jsonrpc: '2.0', id: request.id, method: 'chunk', params: chunk })
        send({ jsonrpc: '2.0', id: request.id, method: 'end', params: {} })
      } catch (error) {
        send({ jsonrpc: '2.0', id: request.id, method: 'error', params: { message: error instanceof Error ? error.message : String(error), data: error instanceof BusinessError ? { kind: error.kind, retryAfter: error.retryAfter } : undefined } })
      }
      return
    }
    throw new Error(`unknown method: ${request.method}`)
  }
}

main().catch(error => {
  process.stderr.write(error instanceof Error ? error.message : String(error))
  process.exitCode = 1
})
