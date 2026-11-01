/** Test-only plugin: all traffic is restricted by the harness to a loopback upstream. */
export default async function(context) {
  const cfg=context.config
  async function call(path,body,signal) {
    const response=await fetch(cfg.baseUrl+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal})
    if(!response.ok) throw Error('test upstream failure')
    return response.json()
  }
  return {
    init:()=>({models:[{id:'mock'}]}),
    completion:(params,signal)=>call('/chat',params,signal),
    stream:async function*(params,signal) {
      const result=await call('/chat',params,signal)
      yield JSON.stringify({id:'test-stream',choices:[{index:0,delta:{content:'hello'},finish_reason:'stop'}],usage:result.usage})
    },
    healthCheck:async()=>({reachable:true,authenticated:true,modelsReady:true,accountReady:true}),
    validateCredential:async(credential,signal)=>call('/validate',credential,signal),
    queryQuota:(credential,signal)=>call('/quota',credential,signal),
    actions:Object.fromEntries((context.spec.actions??[]).filter(a=>a.id!=='unsupported').map(action=>[action.id,(params,signal)=>call('/action',{...params,action:action.id},signal)])),
  }
}
