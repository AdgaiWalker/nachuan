import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {checkSchema, validate} from './schema.js'
const fixtures=JSON.parse(readFileSync(new URL('../../contracts/schema-fixtures.json',import.meta.url),'utf8'))
for (const f of fixtures) test(`shared schema: ${f.name}`,()=>{
  if(f.schemaValid===false) { assert.throws(()=>checkSchema(f.schema)); return }
  checkSchema(f.schema)
  if(f.valid) validate(f.value,f.schema)
  else assert.throws(()=>validate(f.value,f.schema))
})
