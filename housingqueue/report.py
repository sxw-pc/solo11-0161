import hashlib
import json
from .store import connect,many,one
from .source import ingest
TABLES=['household','member','document','application_decision','income_check','property_check','objection','eligibility','queue_position','unit','offer','signature','allocation_dispute','decision_log',
 'defect','inspection','decommission','work_order','reinspection','accountability','temporary_placement','reoccupation','permanent_replacement','safety_dispute']
def report(db):
    r={t:[dict(x) for x in many(db,f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}
    r['event']=[dict(x) for x in many(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    r['source_file']=[dict(x) for x in many(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    r['placement_adjudication']=placement_adjudication(db)
    return r
def placement_adjudication(db):
    """Three obligations that must never be flattened into the latest household.status:
    the qualification at original lease signature, the current replacement
    qualification, and the temporary safety-placement duty."""
    household_ids={x['household_id'] for x in many(db,'SELECT DISTINCT household_id FROM offer WHERE state IN ("signed","signed_disputed")')}
    household_ids|={x['household_id'] for x in many(db,'SELECT DISTINCT household_id FROM temporary_placement')}
    household_ids|={x['household_id'] for x in many(db,'SELECT DISTINCT household_id FROM permanent_replacement')}
    out=[]
    for hid in sorted(household_ids):
        lease=one(db,"""SELECT o.id AS offer_id,o.unit_id,o.eligibility_version,e.state AS eligibility_state
                        FROM offer o LEFT JOIN eligibility e ON e.household_id=o.household_id AND e.version=o.eligibility_version
                        WHERE o.household_id=? AND o.state IN ('signed','signed_disputed') ORDER BY o.rowid LIMIT 1""",(hid,))
        current=one(db,'SELECT state,reason,version FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(hid,))
        pledge=one(db,'SELECT * FROM permanent_replacement WHERE household_id=? ORDER BY rowid DESC LIMIT 1',(hid,))
        placement=one(db,"SELECT * FROM temporary_placement WHERE household_id=? ORDER BY rowid DESC LIMIT 1",(hid,))
        dispute=one(db,"SELECT * FROM safety_dispute WHERE household_id=? ORDER BY rowid DESC LIMIT 1",(hid,))
        if placement and placement['state']=='active':duty='duty_active'
        elif (dispute and dispute['state']=='open') or (pledge and not pledge['completed_at']):duty='duty_pending'
        else:duty='duty_closed'
        out.append({
          'household_id':hid,
          'original_lease_eligibility':None if not lease else {
            'offer_id':lease['offer_id'],'unit_id':lease['unit_id'],
            'eligibility_version':lease['eligibility_version'],'state':lease['eligibility_state']},
          'current_replacement_eligibility':None if not current else {
            'state':current['state'],'version':current['version'],'reason':current['reason'],
            'pledge_state':pledge['state'] if pledge else None,
            'pledged_eligibility_version':pledge['eligibility_version'] if pledge else None,
            'replacement_unit_id':pledge['replacement_unit_id'] if pledge else None,
            'completed_at':pledge['completed_at'] if pledge else None},
          'temporary_safety_placement_duty':{
            'state':duty,
            'placement_id':placement['id'] if placement else None,
            'swing_unit_id':placement['swing_unit_id'] if placement else None,
            'original_unit_id':placement['original_unit_id'] if placement else None,
            'from_at':placement['from_at'] if placement else None,
            'ended_at':placement['ended_at'] if placement else None,
            'safety_dispute':dispute['state'] if dispute else None},
        })
    return out
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
    # placement_adjudication is a derived view, not a source ledger: it is rebuilt in
    # both reports from the base tables and excluded from the legacy table diff so a
    # forged base row is reported once under that table's name.
    base_keys=[k for k in old if k!='placement_adjudication']
    mismatches=[k for k in base_keys if old[k]!=new[k]]
    replay.close()
    return {'replay_ok':not mismatches,'mismatches':mismatches,'event_count':len(old['event'])}
