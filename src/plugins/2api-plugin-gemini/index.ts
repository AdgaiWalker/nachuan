import { openAIBusiness } from '../../plugin-runtime/dist/openai.js'
import type { PluginFactory } from '../../plugin-runtime/dist/contracts.js'

// Gemini 的 search 请求转换属于业务插件，Python 内核不认识此平台。
const factory: PluginFactory = openAIBusiness([
  { id: 'gemini-auto' }, { id: 'gemini-2.5-pro' }, { id: 'gemini-3.5-flash' },
  { id: 'gemini-3.1-pro-preview' }, { id: 'gemini-imagen' }, { id: 'gemini-veo' },
])
export default factory
