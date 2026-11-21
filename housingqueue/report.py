import hashlib
import json
from .store import connect,many
from .source import ingest
TABLES=['household','member','document','application_decision','income_check','property_check','objection','eligibility','queue_position','unit','offer','signature','allocation_dispute','shuttle_vehicle','shuttle_request','shuttle_decision','decision_log']
def report(db):
    r={t:[dict(x) for x in many(db,f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}
    r['event']=[dict(x) for x in many(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    r['source_file']=[dict(x) for x in many(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    return r
def audit(db,handlers):
    replay=connect(':memory:')
    for src in many(db,'SELECT command,sha,raw FROM source_file ORDER BY id'):
        if hashlib.sha256(src['raw']).hexdigest()!=src['sha']:return {'replay_ok':False,'mismatches':['source_file_sha']}
        with replay:
            for e in json.loads(src['raw'])['events']:
                if e['kind'] not in handlers[src['command']]:raise ValueError('recorded command/kind mismatch')
                if ingest(replay,e):handlers[src['command']][e['kind']](replay,e)
            replay.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(src['command'],src['sha'],src['raw']))
    old,new=report(db),report(replay)
    for r in (old,new):
        for x in r['decision_log']:x.pop('at',None)
    replay.close()
    return {'replay_ok':old==new,'mismatches':[k for k in old if old[k]!=new[k]],'event_count':len(old['event'])}
