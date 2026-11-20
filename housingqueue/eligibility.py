from .store import one,many,log

INCOME_LIMIT_CENTS=300000
PROPERTY_LIMIT_M2=20

def income(db,e):
    if not one(db,'SELECT id FROM household WHERE id=?',(e['household_id'],)):raise ValueError('unknown household')
    if int(e['cents'])<0 or not e.get('issuer'):raise ValueError('signed nonnegative monthly income required')
    db.execute('INSERT INTO income_check(id,household_id,month,cents,issuer,at) VALUES(?,?,?,?,?,?)',
               (e['id'],e['household_id'],e['month'],int(e['cents']),e['issuer'],e['at']))
    return recompute(db,e['household_id'],e['id'])

def property(db,e):
    if not one(db,'SELECT id FROM household WHERE id=?',(e['household_id'],)):raise ValueError('unknown household')
    if float(e['area_m2'])<0 or not e.get('issuer') or e['effective_at']>e['at']:
        raise ValueError('property check needs valid effective time and issuer')
    db.execute('INSERT INTO property_check(id,household_id,area_m2,effective_at,issuer,at) VALUES(?,?,?,?,?,?)',
               (e['id'],e['household_id'],float(e['area_m2']),e['effective_at'],e['issuer'],e['at']))
    return recompute(db,e['household_id'],e['id'])

def object(db,e):
    if not one(db,'SELECT id FROM household WHERE id=?',(e['household_id'],)):raise ValueError('unknown household')
    if e['opened_at']>=e['closes_at']:raise ValueError('objection deadline must follow opening')
    db.execute('INSERT INTO objection(id,household_id,opened_at,closes_at,reason) VALUES(?,?,?,?,?)',
               (e['id'],e['household_id'],e['opened_at'],e['closes_at'],e['reason']))
    return recompute(db,e['household_id'],e['id'])

def resolve(db,e):
    o=one(db,'SELECT * FROM objection WHERE id=?',(e['objection_id'],))
    if not o or e['at']<o['opened_at'] or not e.get('reviewer'):raise ValueError('independent objection resolution required')
    db.execute('UPDATE objection SET resolved_at=?,reviewer=? WHERE id=?',(e['at'],e['reviewer'],o['id']))
    return recompute(db,o['household_id'],e['id'])

def recompute(db,household_id,event_id=None):
    h=one(db,'SELECT * FROM household WHERE id=?',(household_id,))
    if not h:return None
    app=one(db,'SELECT * FROM application_decision WHERE household_id=? ORDER BY id DESC LIMIT 1',(household_id,))
    incomes=many(db,'SELECT * FROM income_check WHERE household_id=? ORDER BY month,at,id',(household_id,))
    latest_by_month={i['month']:i for i in incomes}
    prop=one(db,'SELECT * FROM property_check WHERE household_id=? ORDER BY effective_at DESC,at DESC,id DESC LIMIT 1',(household_id,))
    objections=many(db,'SELECT * FROM objection WHERE household_id=? AND resolved_at IS NULL',(household_id,))
    if not app or app['state']!='accepted':state,reason='pending_application','application not accepted'
    elif len(latest_by_month)<3:state,reason='pending_income','three monthly income checks required'
    elif not prop:state,reason='pending_property','property-rights verification absent'
    elif sum(x['cents'] for x in latest_by_month.values())/len(latest_by_month)>INCOME_LIMIT_CENTS:
        state,reason='ineligible_income','monthly household income above threshold'
    elif prop['area_m2']>PROPERTY_LIMIT_M2:state,reason='ineligible_property','owned area above threshold'
    elif objections:state,reason='frozen_objection','open objection prevents effective qualification'
    else:state,reason='qualified','income and property checks support effective qualification'
    members=one(db,"SELECT COUNT(*) AS n FROM member WHERE household_id=? AND state='active'",(household_id,))['n']
    priority=10 if members>=4 else 5 if members>=2 else 1
    old=one(db,'SELECT * FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(household_id,))
    if not old or (old['state'],old['priority'])!=(state,priority):
        version=1 if not old else old['version']+1
        db.execute('INSERT INTO eligibility(household_id,state,priority,reason,version) VALUES(?,?,?,?,?)',(household_id,state,priority,reason,version))
        log(db,'eligibility',household_id,state,reason,event_id)
        recompute_queue(db)
        from .allocation import reconcile_offers
        reconcile_offers(db,household_id)
    return state

def recompute_queue(db):
    eligible=many(db,"SELECT e.household_id,e.priority,a.accepted_at FROM eligibility e JOIN application_decision a ON a.household_id=e.household_id JOIN household h ON h.id=e.household_id WHERE e.id=(SELECT MAX(id) FROM eligibility WHERE household_id=e.household_id) AND a.id=(SELECT MAX(id) FROM application_decision WHERE household_id=a.household_id) AND e.state='qualified' AND h.status NOT IN ('housed','housed_disputed') ORDER BY e.priority DESC,a.accepted_at,e.household_id")
    for rank,x in enumerate(eligible,1):
        old=one(db,'SELECT * FROM queue_position WHERE household_id=? ORDER BY id DESC LIMIT 1',(x['household_id'],))
        if not old or old['rank']!=rank or old['state']!='waiting':
            db.execute('INSERT INTO queue_position(household_id,rank,state,reason) VALUES(?,?,?,?)',(x['household_id'],rank,'waiting','priority then complete-evidence receipt'))
