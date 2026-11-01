/** nachuan-schema/1. Values are never interpolated into errors. */
export type Schema = Record<string, any>
const keys = new Set(['anyOf','type','properties','required','additionalProperties','enum','minLength','maxLength','minimum','maximum','items','minItems','maxItems','title','description','secret'])
export function checkSchema(schema: Schema, depth = 0): void {
  if (depth > 16 || !schema || typeof schema !== 'object' || Array.isArray(schema) || Object.keys(schema).some(k=>!keys.has(k))) throw Error('unsupported schema')
  if ('anyOf' in schema) {
    if (!Array.isArray(schema.anyOf) || !schema.anyOf.length) throw Error('invalid anyOf')
    for (const branch of schema.anyOf) checkSchema(branch,depth+1)
  }
  if (!['object','array','string','number','integer','boolean','null'].includes(schema.type)) throw Error('schema type required')
  if ('enum' in schema && (!Array.isArray(schema.enum) || !schema.enum.length)) throw Error('invalid enum')
  for (const k of ['minLength','maxLength','minItems','maxItems']) if (k in schema && (!Number.isInteger(schema[k]) || schema[k]<0)) throw Error('invalid bound')
  for (const k of ['minimum','maximum']) if (k in schema && (typeof schema[k]!=='number' || !Number.isFinite(schema[k]))) throw Error('invalid bound')
  for (const [lo,hi] of [['minLength','maxLength'],['minItems','maxItems'],['minimum','maximum']]) if (lo! in schema && hi! in schema && schema[lo!]>schema[hi!]) throw Error('reversed bound')
  if (schema.type==='object') {
    const props=schema.properties??{}, required=schema.required??[]
    if (!props || typeof props!=='object' || Array.isArray(props) || !Array.isArray(required) || required.some(k=>typeof k!=='string' || !Object.hasOwn(props,k))) throw Error('invalid object schema')
    if ('additionalProperties' in schema && typeof schema.additionalProperties!=='boolean') throw Error('invalid additionalProperties')
    for (const child of Object.values(props)) checkSchema(child as Schema,depth+1)
  }
  if (schema.type==='array') checkSchema(schema.items,depth+1)
}
export function validate(value: any, schema: Schema, path='$'): void {
  if (schema.anyOf && !schema.anyOf.some((branch:Schema)=>{try {validate(value,branch,path);return true} catch {return false}})) throw Error(`${path}: anyOf`)
  const kind=schema.type
  const valid: Record<string,boolean> = {object:value!==null && typeof value==='object' && !Array.isArray(value),array:Array.isArray(value),string:typeof value==='string',number:typeof value==='number' && Number.isFinite(value),integer:Number.isInteger(value),boolean:typeof value==='boolean',null:value===null}
  if (!valid[kind]) throw Error(`${path}: type`)
  const canonical=(x:any): any => Array.isArray(x) ? x.map(canonical) : x!==null && typeof x==='object' ? Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])) : x
  if (schema.enum && !schema.enum.some((x:any)=>typeof x===typeof value && JSON.stringify(canonical(x))===JSON.stringify(canonical(value)))) throw Error(`${path}: enum`)
  if (kind==='object') {
    const props=schema.properties??{}
    if ((schema.required??[]).some((k:string)=>!Object.hasOwn(value,k))) throw Error(`${path}: required`)
    if (schema.additionalProperties===false && Object.keys(value).some(k=>!Object.hasOwn(props,k))) throw Error(`${path}: additionalProperties`)
    for (const [k,v] of Object.entries(value)) if (Object.hasOwn(props,k)) validate(v,props[k],path+'.'+k)
  }
  if (kind==='array') {
    if (value.length<(schema.minItems??0) || value.length>(schema.maxItems??Infinity)) throw Error(`${path}: length`)
    for (const item of value) validate(item,schema.items,path+'[]')
  }
  if (kind==='string' && ([...value].length<(schema.minLength??0) || [...value].length>(schema.maxLength??Infinity))) throw Error(`${path}: length`)
  if (['number','integer'].includes(kind) && (value<(schema.minimum??-Infinity) || value>(schema.maximum??Infinity))) throw Error(`${path}: range`)
}
