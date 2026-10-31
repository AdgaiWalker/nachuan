import { openAIBusiness } from '../../plugin-runtime/dist/openai.js'
import type { PluginFactory } from '../../plugin-runtime/dist/contracts.js'

const factory: PluginFactory = openAIBusiness([
  { id: 'glm-4' }, { id: 'glm-4.7' }, { id: 'glm-5' },
])
export default factory
