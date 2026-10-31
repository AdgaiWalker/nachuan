import type { OpenAIChunk } from './contracts.js'

/** 平台响应到 OpenAI 流的业务转换；Python 负责通用分帧与思考状态。 */
export function normalizeChunk(value: unknown, model: string): OpenAIChunk {
  const source = (value && typeof value === 'object' ? value : {}) as Record<string, any>
  const choice = source.choices?.[0] ?? {}
  const delta = choice.delta ?? {}
  const part = source.candidates?.[0]?.content?.parts?.[0] ?? {}
  const content = delta.content ?? part.text ?? source.content ?? ''
  const reasoning = delta.reasoning_content ?? delta.reasoning ?? (part.thought ? part.text : '') ?? ''
  const toolCalls = delta.tool_calls ?? source.tool_calls ?? source.toolCalls
  return {
    id: String(source.id ?? `chatcmpl-${crypto.randomUUID().replaceAll('-', '')}`),
    object: 'chat.completion.chunk',
    created: Number(source.created ?? Math.floor(Date.now() / 1000)),
    model: String(source.model ?? model),
    choices: [{
      index: Number(choice.index ?? 0),
      delta: {
        ...(content ? { content: String(content) } : {}),
        ...(reasoning ? { reasoning_content: String(reasoning) } : {}),
        ...(toolCalls ? { tool_calls: toolCalls } : {}),
        ...(choice.finish_reason === null && Object.keys(delta).length === 0 ? { role: 'assistant' as const } : {}),
      },
      finish_reason: choice.finish_reason ?? null,
    }],
    ...(source.usage ? { usage: source.usage } : {}),
  } as OpenAIChunk
}

export async function* sseData(response: Response): AsyncIterable<string> {
  if (!response.body) throw new Error('上游没有响应流')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
        if (data) yield data
      }
      if (done) break
    }
    if (buffer.trim()) {
      const data = buffer.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
      if (data) yield data
    }
  } finally { reader.releaseLock() }
}
