from datetime import date
from .store import one,many,log

REQUIRED={'identity','residence','relation'}

def household(db,e):
    old=one(db,'SELECT * FROM household WHERE id=?',(e['household_id'],))
    if old and int(e['version'])<=old['version']:
        if int(e['version'])==old['version'] and old['community']!=e['community']:
            log(db,'application',e['household_id'],'version_conflict','parallel community identity',e['id'])
            return 'version_conflict'
        return 'stale'
    db.execute('INSERT INTO household(id,community,applied_at,residence_from,status,reason,version) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET community=excluded.community,applied_at=excluded.applied_at,residence_from=excluded.residence_from,version=excluded.version',
               (e['household_id'],e['community'],e['applied_at'],e['residence_from'],'pending','awaiting complete evidence',int(e['version'])))
    return recompute(db,e['household_id'],e['id'])

def member(db,e):
    h=one(db,'SELECT * FROM household WHERE id=?',(e['household_id'],))
    if not h:raise ValueError('unknown household')
    if e['relation'] not in {'applicant','spouse','child','parent'}:raise ValueError('invalid household relation')
    db.execute('INSERT INTO member(id,household_id,relation,joined_at,state) VALUES(?,?,?,?,?)',
               (e['person_id'],e['household_id'],e['relation'],e['joined_at'],'active'))
    others=many(db,"SELECT household_id FROM member WHERE id=? AND household_id<>? AND state='active'",(e['person_id'],e['household_id']))
    for x in others:recompute(db,x['household_id'],e['id'])
    return recompute(db,e['household_id'],e['id'])

def remove_member(db,e):
    h=one(db,'SELECT * FROM household WHERE id=?',(e['household_id'],))
    if not h:raise ValueError('unknown household')
    m=one(db,"SELECT * FROM member WHERE id=? AND household_id=? AND state='active'",(e['person_id'],e['household_id']))
    if not m:raise ValueError('unknown active member')
    db.execute("UPDATE member SET state='removed',removed_at=? WHERE id=? AND household_id=?",(e['at'],e['person_id'],e['household_id']))
    log(db,'application',e['household_id'],'member_removed','membership corrected: '+e.get('reason',''),e['id'])
    others=many(db,"SELECT household_id FROM member WHERE id=? AND household_id<>? AND state='active'",(e['person_id'],e['household_id']))
    state=recompute(db,e['household_id'],e['id'])
    for x in others:recompute(db,x['household_id'],e['id'])
    from . import facility
    facility.apply_person_correction(db,e['person_id'],e['household_id'],e['at'],e['id'])
    return state

def document(db,e):
    if not one(db,'SELECT id FROM household WHERE id=?',(e['household_id'],)):raise ValueError('unknown household')
    if e['issued_at']>e['received_at'] or e['received_at']>=e['expires_at'] or not e.get('issuer'):
        raise ValueError('dated issuer document and receipt required')
    db.execute('INSERT INTO document(id,household_id,kind,subject,issued_at,received_at,expires_at,issuer,state) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'],e['household_id'],e['doc_kind'],e['subject'],e['issued_at'],e['received_at'],e['expires_at'],e['issuer'],'valid'))
    return recompute(db,e['household_id'],e['id'])

def revoke_document(db,e):
    d=one(db,'SELECT * FROM document WHERE id=?',(e['document_id'],))
    if not d:raise ValueError('unknown document')
    db.execute("UPDATE document SET state='revoked' WHERE id=?",(d['id'],))
    return recompute(db,d['household_id'],e['id'])

def recompute(db,household_id,event_id=None):
    h=one(db,'SELECT * FROM household WHERE id=?',(household_id,))
    if not h:raise ValueError('unknown household')
    members=many(db,"SELECT * FROM member WHERE household_id=? AND state='active'",(household_id,))
    docs=many(db,"SELECT * FROM document WHERE household_id=? AND state='valid' AND issued_at<=received_at AND expires_at>received_at",(household_id,))
    kinds={d['kind'] for d in docs}
    shared=[m['id'] for m in members if one(db,"SELECT id FROM member WHERE id=? AND household_id<>? AND state='active'",(m['id'],household_id))]
    if shared:state,reason='identity_conflict','member simultaneously claimed by another household'
    elif not members or sum(m['relation']=='applicant' for m in members)!=1:state,reason='pending','one applicant member required'
    elif not REQUIRED.issubset(kinds):state,reason='pending','required identity, residence and relation evidence absent'
    elif (date.fromisoformat(h['applied_at'][:10])-date.fromisoformat(h['residence_from'][:10])).days<1095:
        state,reason='ineligible_residence','continuous residence shorter than three years'
    else:state,reason='accepted','complete and identity-consistent household application'
    accepted_at=max([h['applied_at'],*[d['received_at'] for d in docs]]) if state=='accepted' else None
    old=one(db,'SELECT * FROM application_decision WHERE household_id=? ORDER BY id DESC LIMIT 1',(household_id,))
    if not old or (old['state'],old['accepted_at'])!=(state,accepted_at):
        db.execute('INSERT INTO application_decision(household_id,state,accepted_at,reason) VALUES(?,?,?,?)',
                   (household_id,state,accepted_at,reason))
        db.execute('UPDATE household SET status=?,reason=? WHERE id=?',(state,reason,household_id))
        log(db,'application',household_id,state,reason,event_id)
        from .eligibility import recompute as recompute_eligibility
        recompute_eligibility(db,household_id)
        if one(db,"SELECT id FROM offer WHERE household_id=? AND state='signed_disputed' LIMIT 1",(household_id,)):
            db.execute("UPDATE household SET status='housed_disputed',reason='signed allocation retained under late evidence dispute' WHERE id=?",(household_id,))
    return state
