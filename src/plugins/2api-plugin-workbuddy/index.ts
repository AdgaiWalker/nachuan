import { openAIBusiness } from '../../plugin-runtime/dist/openai.js'
import type { PluginFactory } from '../../plugin-runtime/dist/contracts.js'

const factory: PluginFactory = openAIBusiness([
  { id: 'glm-5.2' }, { id: 'glm-5.3' }, { id: 'deepseek-v3' },
  { id: 'deepseek-r1' }, { id: 'deepseek-v4.1-flash' },
])
export default factory
