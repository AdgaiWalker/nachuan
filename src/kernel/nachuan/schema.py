"""nachuan-schema/1: deliberately bounded, identical Python/TS validation."""
import math

KEYS = {'anyOf','type','properties','required','additionalProperties','enum','minLength','maxLength','minimum','maximum','items','minItems','maxItems','title','description','secret'}
TYPES = {'object','array','string','number','integer','boolean','null'}


def check_schema(schema, depth=0):
    if depth > 16 or not isinstance(schema, dict) or set(schema) - KEYS:
        raise ValueError('unsupported schema')
    if 'anyOf' in schema:
        if not isinstance(schema['anyOf'],list) or not schema['anyOf']: raise ValueError('invalid anyOf')
        for branch in schema['anyOf']: check_schema(branch,depth+1)
    kind = schema.get('type')
    if not isinstance(kind,str) or kind not in TYPES:
        raise ValueError('schema type required')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise ValueError('invalid enum')
    for key in ('minLength','maxLength','minItems','maxItems'):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError('invalid schema bound')
    for key in ('minimum','maximum'):
        if key in schema and (type(schema[key]) not in (int,float) or not math.isfinite(schema[key])):
            raise ValueError('invalid schema bound')
    for lo, hi in [('minLength','maxLength'),('minItems','maxItems'),('minimum','maximum')]:
        if lo in schema and hi in schema and schema[lo] > schema[hi]:
            raise ValueError('reversed schema bound')
    if kind == 'object':
        props = schema.get('properties', {})
        required = schema.get('required', [])
        if not isinstance(props, dict) or not isinstance(required, list) or any(not isinstance(x,str) or x not in props for x in required):
            raise ValueError('invalid object schema')
        if type(schema.get('additionalProperties', True)) is not bool:
            raise ValueError('additionalProperties must be boolean')
        for value in props.values(): check_schema(value, depth+1)
    if kind == 'array':
        if 'items' not in schema: raise ValueError('array items required')
        check_schema(schema['items'], depth+1)


def json_equal(a,b):
    if type(a) is bool or type(b) is bool: return type(a) is type(b) and a == b
    if type(a) in (int,float) and type(b) in (int,float): return a == b
    if type(a) is not type(b): return False
    if isinstance(a,dict): return a.keys() == b.keys() and all(json_equal(a[k],b[k]) for k in a)
    if isinstance(a,list): return len(a)==len(b) and all(json_equal(x,y) for x,y in zip(a,b))
    return a == b


def validate(value, schema, path='$'):
    """Error paths contain field names only; never interpolate secret values."""
    if 'anyOf' in schema:
        for branch in schema['anyOf']:
            try: validate(value,branch,path)
            except ValueError: continue
            break
        else: raise ValueError(f'{path}: anyOf')
    kind = schema['type']
    valid = {'object':isinstance(value,dict), 'array':isinstance(value,list), 'string':isinstance(value,str),
             'number':type(value) in (int,float), 'integer':type(value) in (int,float) and math.isfinite(value) and int(value)==value,
             'boolean':type(value) is bool, 'null':value is None}[kind]
    if not valid or (kind in ('number','integer') and not math.isfinite(value)):
        raise ValueError(f'{path}: type')
    if 'enum' in schema and not any(json_equal(value,x) for x in schema['enum']):
        raise ValueError(f'{path}: enum')
    if kind == 'object':
        props=schema.get('properties',{})
        if any(key not in value for key in schema.get('required',[])): raise ValueError(f'{path}: required')
        if not schema.get('additionalProperties',True) and set(value)-set(props): raise ValueError(f'{path}: additionalProperties')
        for key, item in value.items():
            if key in props: validate(item,props[key],path+'.'+key)
    if kind == 'array':
        if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',float('inf')): raise ValueError(f'{path}: length')
        for item in value: validate(item,schema['items'],path+'[]')
    if kind == 'string' and (len(value)<schema.get('minLength',0) or len(value)>schema.get('maxLength',float('inf'))): raise ValueError(f'{path}: length')
    if kind in ('number','integer') and (value<schema.get('minimum',-float('inf')) or value>schema.get('maximum',float('inf'))): raise ValueError(f'{path}: range')


OBJECT = {'type':'object'}
LEGACY_CREDENTIAL = {
    'type':'object',
    'properties':{key:{'type':'string','minLength':1} for key in ('apiKey','accessToken')},
    'anyOf':[{'type':'object','properties':{key:{'type':'string'}},'required':[key]} for key in ('apiKey','accessToken')],
}


def protect(value, schema):
    """Public action results respect secret annotations, including non-string values."""
    if schema.get('secret'):
        return '••••'
    for branch in schema.get('anyOf', []):
        value = protect(value, branch)
    if isinstance(value,dict):
        props=schema.get('properties',{})
        return {key:protect(item,props.get(key,{})) for key,item in value.items()}
    if isinstance(value,list):
        return [protect(item,schema.get('items',{})) for item in value]
    return value


class SchemaMetadata(dict):
    """Host-created marker for validated descriptor schemas, never decoded from RPC JSON."""
