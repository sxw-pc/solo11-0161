import hashlib
import json
from pathlib import Path
from .store import one
def read_file(path):
    raw=Path(path).read_bytes();data=json.loads(raw)
    if not isinstance(data,dict) or not isinstance(data.get('events'),list):raise ValueError('events array required')
    return raw,data['events']
def ingest(db,e):
    raw=json.dumps(e,sort_keys=True,separators=(',',':')).encode();sha=hashlib.sha256(raw).hexdigest()
    old=one(db,'SELECT kind,sha FROM event WHERE id=?',(e['id'],))
    if old:
        if old['kind']!=e['kind'] or old['sha']!=sha:raise ValueError('immutable event id conflict')
        return False
    db.execute('INSERT INTO event(id,kind,sha,raw) VALUES(?,?,?,?)',(e['id'],e['kind'],sha,raw))
    return True
