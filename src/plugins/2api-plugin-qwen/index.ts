import { openAIBusiness } from '../../plugin-runtime/dist/openai.js'
import type { PluginFactory } from '../../plugin-runtime/dist/contracts.js'

const factory: PluginFactory = openAIBusiness([
  { id: 'qwen-max' }, { id: 'qwen-plus' }, { id: 'qwen-turbo' }, { id: 'qwen-long' },
])
export default factory
