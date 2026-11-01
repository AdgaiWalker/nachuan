"""Versioned host control plane; public responses never expose credential values."""
from aiohttp import web
from .errors import KernelError
from .credentials import GenerationConflict
from .schema import validate


def routes(app,key):
    def kernel(request): return request.app[key]
    def plugin(request): return kernel(request).plugin(request.match_info['name'])
    def response(request,data,status=200): return web.json_response(kernel(request).public(data),status=status)
    async def descriptor(request): return response(request,kernel(request).descriptor(plugin(request)))
    async def models(request):
        k=kernel(request)
        return response(request,{'models':[{'plugin':p.name,'available':p.status.value=='running',**m} for p in k._snapshot.plugins.values() for m in k.descriptor(p)['models']]})
    async def accounts(request):
        p=plugin(request)
        return response(request,{'accounts':p.account_pool.snapshot() if p.account_pool else []})
    async def add(request):
        k=kernel(request); p=plugin(request); body=await request.json()
        if not isinstance(body,dict): raise ValueError('object required')
        credential=body.get('credential',{'apiKey':body.get('apiKey')})
        validate(credential,p.spec.credential_schema)
        await validate_business(k,p,credential)
        if not p.account_pool:
            from .accounts import AccountPool
            p.account_pool=AccountPool(p.directory/'credentials',credential_schema=p.spec.credential_schema)
            p.account_pool.load()
        await p.account_pool.add(body.get('id'),credential,max_in_flight=body.get('maxInFlight',2),enabled=body.get('enabled',True),refresh_enabled=body.get('refreshEnabled'))
        k.register_secrets(p)
        await k.load()
        return response(request,{'ok':True},201)
    async def rotate(request):
        k=kernel(request); p=plugin(request); uid=request.match_info['uid']; body=await request.json()
        if not isinstance(body,dict): raise ValueError('object required')
        if not p.account_pool or uid not in p.account_pool._accounts: raise KernelError(404,'account_not_found')
        explicit='credential' in body
        credential=body.get('credential',{'apiKey':body.get('apiKey')})
        validate(credential,p.spec.credential_schema)
        await validate_business(k,p,credential)
        generation=body.get('expectedGeneration')
        if explicit and type(generation) is not int: raise ValueError('expectedGeneration required')
        await p.account_pool.rotate(uid,credential,expected_generation=generation,recovered=True,replace=explicit)
        k.register_secrets(p)
        return response(request,{'ok':True,'generation':p.account_pool._accounts[uid].credential_generation})
    async def patch(request):
        p=plugin(request); body=await request.json()
        if not isinstance(body,dict) or set(body)-{'enabled','maxInFlight','refreshEnabled'}: raise ValueError('invalid account patch')
        if not p.account_pool or request.match_info['uid'] not in p.account_pool._accounts: raise KernelError(404,'account_not_found')
        await p.account_pool.patch(request.match_info['uid'],enabled=body.get('enabled'),max_in_flight=body.get('maxInFlight'),refresh_enabled=body.get('refreshEnabled'))
        account=p.account_pool._accounts[request.match_info['uid']]
        account.refresh_supported=bool(p.capabilities.get('credentialRefresh')) and account.refresh_enabled is not False
        return response(request,{'ok':True})
    async def actions(request): return response(request,{'actions':kernel(request).descriptor(plugin(request))['actions']})
    async def check_plugin_health(request):
        k=kernel(request); p=plugin(request)
        if not p.transport or not p.account_pool or p.status.value != 'running':
            raise KernelError(409,'plugin_not_ready')
        account=next((item for item in p.account_pool._accounts.values() if item.enabled),None)
        if account is None: raise KernelError(409,'account_not_ready')
        fields=('reachable','authenticated','modelsReady','accountReady')
        try:
            async with k.maintenance(p,account.id):
                evidence=await p.transport.call('credential.health',{'credential':{'id':account.id,'secret':account.credential()}},15)
            if not isinstance(evidence,dict): raise ValueError('invalid health evidence')
            health={'configured':True,**{field:evidence.get(field) is True for field in fields}}
            health['ready']=all(health.values())
        except Exception:
            health={'configured':True,**{field:False for field in fields},'ready':False,'error':'health_check_failed'}
        return response(request,{'health':health})
    async def start(request):
        body=await request.json()
        if not isinstance(body,dict): raise ValueError('object required')
        result=await kernel(request).actions.start(plugin(request),request.match_info['action'],body.get('accountId'),body.get('input',{}),body.get('idempotencyKey'))
        return response(request,result,202)
    async def run(request): return response(request,kernel(request).actions.get(request.match_info['rid']))
    async def cancel(request): return response(request,await kernel(request).actions.cancel(request.match_info['rid']))
    async def quota(request): return response(request,kernel(request).quota.get(plugin(request),request.match_info['uid']))
    async def refresh(request): return response(request,await kernel(request).quota.refresh(plugin(request),request.match_info['uid']))
    app.router.add_get('/kernel/models',models)
    base='/kernel/plugins/{name}'
    for method,path,handler in [('GET','/descriptor',descriptor),('GET','/accounts',accounts),('POST','/accounts',add),('POST','/accounts/{uid}/rotate',rotate),('PATCH','/accounts/{uid}',patch),('POST','/health/check',check_plugin_health),('GET','/actions',actions),('POST','/actions/{action}/runs',start),('GET','/accounts/{uid}/quota',quota),('POST','/accounts/{uid}/quota/refresh',refresh)]:
        app.router.add_route(method,base+path,handler)
    app.router.add_get('/kernel/action-runs/{rid}',run)
    app.router.add_post('/kernel/action-runs/{rid}/cancel',cancel)


async def validate_business(k,p,credential):
    if p.capabilities.get('credentialValidate'):
        async with k.maintenance(p):
            result=await p.transport.call('credential.validate',{'credential':credential},15)
            if not isinstance(result,dict) or result.get('valid') is not True: raise ValueError('credential rejected')


@web.middleware
async def errors(request,handler):
    try: return await handler(request)
    except web.HTTPException: raise
    except GenerationConflict: return web.json_response({'error':{'code':'credential_generation_conflict'}},status=409)
    except KernelError as exc: return web.json_response({'error':{'code':exc.code}},status=exc.status)
    except FileExistsError: return web.json_response({'error':{'code':'account_exists'}},status=409)
    except ValueError: return web.json_response({'error':{'code':'invalid_request'}},status=400)
    except TimeoutError: return web.json_response({'error':{'code':'timeout'}},status=504)
    except Exception: return web.json_response({'error':{'code':'plugin_unavailable'}},status=503)
