"""Private envelope persistence. Callers hold their account-store lock."""
import json
import os
import re
from pathlib import Path
from .schema import validate

import asyncio
import weakref
_LOCKS = weakref.WeakValueDictionary()

def lock_for(path):
    key=str(Path(path).resolve())
    lock=_LOCKS.get(key)
    if lock is None:
        lock=asyncio.Lock()
        _LOCKS[key]=lock
    return lock

META = {'enabled','maxInFlight','generation','lastRefreshAt','refreshSupported'}


class GenerationConflict(RuntimeError): pass


def account_id(uid):
    if not isinstance(uid,str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}',uid) or uid.casefold() == 'state':
        raise ValueError('invalid account id')
    return uid


def read(path):
    raw=json.loads(Path(path).read_text())
    if not isinstance(raw,dict): raise ValueError('invalid credential file')
    if raw.get('formatVersion') == 2:
        if not isinstance(raw.get('secret'),dict) or not isinstance(raw.get('account'),dict): raise ValueError('invalid credential envelope')
        account=raw['account']
        if type(raw.get('generation')) is not int or raw['generation'] < 1 or type(account.get('enabled')) is not bool or type(account.get('maxInFlight')) is not int or not 1 <= account['maxInFlight'] <= 1000:
            raise ValueError('invalid credential metadata')
        if 'refreshEnabled' in account and type(account['refreshEnabled']) is not bool: raise ValueError('invalid refresh policy')
        return raw
    return {'formatVersion':2,'generation':raw.get('generation',1),
            'account':{'enabled':raw.get('enabled',True),'maxInFlight':raw.get('maxInFlight',2),**({'refreshEnabled':raw['refreshSupported']} if 'refreshSupported' in raw else {})},
            'updatedAt':raw.get('lastRefreshAt',0),'secret':{key:value for key,value in raw.items() if key not in META}}


def atomic(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.parent.chmod(0o700)
    import tempfile
    fd,tmp=tempfile.mkstemp(prefix='.'+path.stem+'-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,ensure_ascii=False,allow_nan=False)
        os.chmod(tmp,0o600)
        os.replace(tmp,path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def write(path, envelope, schema, *, create=False):
    validate(envelope['secret'],schema)
    path=Path(path)
    if create and path.exists(): raise FileExistsError('account exists')
    if path.exists() and not path.with_suffix('.migration.bak').exists():
        old=json.loads(path.read_text())
        if old.get('formatVersion') != 2:
            atomic(path.with_suffix('.migration.bak'),old)
    atomic(path,envelope)
