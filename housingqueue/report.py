import hashlib
import json
from .store import connect,one,many
from .source import ingest
TABLES=['household','member','document','application_decision','income_check','property_check','objection','eligibility','queue_position','unit','offer','signature','allocation_dispute','facility_room','facility_area','facility_booking','facility_decision','activity','activity_window','guest_pass','facility_session','facility_room_use','facility_checkin','decision_log']
def facility_session_status(db):
    out=[]
    for s in many(db,'SELECT * FROM facility_session ORDER BY rowid'):
        act=one(db,'SELECT * FROM activity WHERE id=?',(s['activity_id'],))
        checks=many(db,"SELECT * FROM facility_checkin WHERE session_id=? AND state='valid' ORDER BY at,id",(s['id'],))
        voids=one(db,"SELECT COUNT(*) AS n FROM facility_checkin WHERE session_id=? AND state='void'",(s['id'],))['n']
        uses=many(db,'SELECT * FROM facility_room_use WHERE session_id=? ORDER BY id',(s['id'],))
        out.append({'session_id':s['id'],'activity_id':s['activity_id'],'household_id':act['household_id'],
          'room_id':s['room_id'],'area_id':s['area_id'],'starts':s['starts'],'ends':s['ends'],'state':s['state'],
          'held':'held' if checks else ('scheduled' if s['state']=='scheduled' else s['state']),
          'goal_window':'primary' if s['starts']==act['starts'] and s['ends']==act['ends'] else 'split',
          'planned_attendees':s['attendees'],'valid_checkins':len(checks),'void_checkins':voids,
          'personnel':[{'person_id':c['person_id'],'source':c['source'],'at':c['at'],'late':c['late'],'witness':c['witness'],'witness_kind':c['witness_kind']} for c in checks],
          'room_responsibility':[{'room_id':u['room_id'],'area_id':u['area_id'],'state':u['state'],'released_at':u['released_at']} for u in uses],
          'needs_equipment':json.loads(s['needs_equipment']),'room_equipment':json.loads(s['room_equipment']),
          'joint_outcome_session':act['outcome_session_id']==s['id']})
    return out
def report(db):
    r={t:[dict(x) for x in many(db,f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}
    r['event']=[dict(x) for x in many(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    r['source_file']=[dict(x) for x in many(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    r['facility_session_status']=facility_session_status(db)
    r['facility_activity_outcome']=[dict(x) for x in many(db,'SELECT id,household_id,attendees,starts,ends,state,outcome,outcome_session_id FROM activity ORDER BY rowid')]
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
    result={'replay_ok':old==new,'mismatches':[k for k in old if old[k]!=new[k]],'event_count':len(old['event'])}
    result['facility_sessions']=[{'session_id':s['session_id'],'starts':s['starts'],'ends':s['ends'],'state':s['state'],'held':s['held'],'valid_checkins':s['valid_checkins']} for s in old['facility_session_status']]
    return result
