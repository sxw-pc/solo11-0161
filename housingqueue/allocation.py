from .store import one,many,log

def unit(db,e):
    if int(e['rooms'])<=0:raise ValueError('unit requires positive rooms')
    db.execute('INSERT INTO unit(id,community,rooms,accessible,available_at,state) VALUES(?,?,?,?,?,?)',
               (e['id'],e['community'],int(e['rooms']),int(bool(e.get('accessible'))),e['available_at'],'available'))
    log(db,'allocation',e['id'],'available','released housing inventory',e['id'])
    return 'available'

def offer(db,e):
    h=one(db,'SELECT * FROM household WHERE id=?',(e['household_id'],))
    u=one(db,'SELECT * FROM unit WHERE id=?',(e['unit_id'],))
    if not h or not u:raise ValueError('household and unit required')
    eligibility=one(db,'SELECT * FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(h['id'],))
    position=one(db,'SELECT * FROM queue_position WHERE household_id=? ORDER BY id DESC LIMIT 1',(h['id'],))
    members=one(db,"SELECT COUNT(*) AS n FROM member WHERE household_id=? AND state='active'",(h['id'],))['n']
    needed_rooms=(members+1)//2
    if e['offered_at']>=e['expires_at'] or e['offered_at']<u['available_at']:
        state,reason='blocked','offer time outside unit availability or deadline'
    elif u['state']!='available':state,reason='blocked','unit already held or assigned'
    elif not eligibility or eligibility['state']!='qualified':state,reason='blocked','qualification frozen or absent'
    elif h['community']!=u['community'] or u['rooms']<needed_rooms:
        state,reason='mismatch','community or household-size room requirement failed'
    elif not position or position['state']!='waiting':state,reason='blocked','household not waiting in current queue'
    else:
        earlier=many(db,"SELECT q.household_id FROM queue_position q JOIN household h ON h.id=q.household_id WHERE q.id=(SELECT MAX(id) FROM queue_position WHERE household_id=q.household_id) AND q.state='waiting' AND q.rank<? AND h.community=? AND h.status NOT IN ('housed','housed_disputed')",(position['rank'],u['community']))
        compatible=[x for x in earlier if (one(db,"SELECT COUNT(*) AS n FROM member WHERE household_id=? AND state='active'",(x['household_id'],))['n']+1)//2<=u['rooms']]
        if compatible:state,reason='out_of_turn','earlier qualified household can use this unit'
        else:state,reason='offered','matching unit reserved for next eligible household'
    db.execute('INSERT INTO offer(id,household_id,unit_id,offered_at,expires_at,state,reason,eligibility_version) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'],h['id'],u['id'],e['offered_at'],e['expires_at'],state,reason,eligibility['version'] if eligibility else 0))
    if state=='offered':
        db.execute("UPDATE unit SET state='held' WHERE id=?",(u['id'],))
        db.execute("INSERT INTO queue_position(household_id,rank,state,reason) VALUES(?,?,?,?)",(h['id'],position['rank'],'offered','unit reserved pending signature'))
    log(db,'allocation',e['id'],state,reason,e['id'])
    return state

def sign(db,e):
    o=one(db,'SELECT * FROM offer WHERE id=?',(e['offer_id'],))
    if not o:raise ValueError('unknown offer')
    applicant=one(db,"SELECT id FROM member WHERE household_id=? AND relation='applicant' AND state='active'",(o['household_id'],))
    eligibility=one(db,'SELECT * FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(o['household_id'],))
    if o['state']!='offered':state,reason='invalid','offer not open for signature'
    elif e['at']>o['expires_at'] or e['at']<o['offered_at']:state,reason='expired','signature outside offer window'
    elif not applicant or e['signer']!=applicant['id']:state,reason='invalid','household applicant must sign'
    elif eligibility['state']!='qualified' or eligibility['version']!=o['eligibility_version']:
        state,reason='frozen','qualification changed after offer'
    else:state,reason='signed','current qualified applicant accepted allocation'
    db.execute('INSERT INTO signature(id,offer_id,at,signer,state,reason) VALUES(?,?,?,?,?,?)',
               (e['id'],o['id'],e['at'],e['signer'],state,reason))
    if state=='signed':
        db.execute("UPDATE offer SET state='signed',reason=? WHERE id=?",(reason,o['id']))
        db.execute("UPDATE unit SET state='assigned' WHERE id=?",(o['unit_id'],))
        db.execute("UPDATE household SET status='housed',reason='signed allocation fulfilled' WHERE id=?",(o['household_id'],))
        db.execute("INSERT INTO queue_position(household_id,rank,state,reason) VALUES(?,?,?,?)",(o['household_id'],0,'housed','signed unit allocation'))
        log(db,'application',o['household_id'],'housed','allocation changed household status',e['id'])
    log(db,'allocation',o['id'],state,reason,e['id'])
    return state

def expire(db,e):
    o=one(db,'SELECT * FROM offer WHERE id=?',(e['offer_id'],))
    if not o:raise ValueError('unknown offer')
    if o['state']!='offered' or e['at']<=o['expires_at']:raise ValueError('only overdue active offer may expire')
    db.execute("UPDATE offer SET state='expired',reason='signature deadline passed' WHERE id=?",(o['id'],))
    db.execute("UPDATE unit SET state='available' WHERE id=?",(o['unit_id'],))
    eligibility=one(db,'SELECT state FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(o['household_id'],))
    if eligibility and eligibility['state']=='qualified':
        prior=one(db,'SELECT rank FROM queue_position WHERE household_id=? ORDER BY id DESC LIMIT 1',(o['household_id'],))
        db.execute("INSERT INTO queue_position(household_id,rank,state,reason) VALUES(?,?,?,?)",(o['household_id'],prior['rank'],'waiting','expired offer releases unit'))
    log(db,'allocation',o['id'],'expired','unit released for next household',e['id'])
    return 'expired'

def reconcile_offers(db,household_id):
    eligibility=one(db,'SELECT * FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(household_id,))
    if not eligibility or eligibility['state']=='qualified':return
    for o in many(db,"SELECT * FROM offer WHERE household_id=? AND state='offered'",(household_id,)):
        db.execute("UPDATE offer SET state='frozen',reason='qualification changed after offer' WHERE id=?",(o['id'],))
        db.execute("UPDATE unit SET state='available' WHERE id=?",(o['unit_id'],))
        prior=one(db,'SELECT rank FROM queue_position WHERE household_id=? ORDER BY id DESC LIMIT 1',(household_id,))
        db.execute('INSERT INTO queue_position(household_id,rank,state,reason) VALUES(?,?,?,?)',
                   (household_id,prior['rank'] if prior else 0,'frozen','qualification revision blocks allocation'))
        log(db,'allocation',o['id'],'frozen','qualification revision released temporary unit hold')
    for o in many(db,"SELECT * FROM offer WHERE household_id=? AND state='signed'",(household_id,)):
        db.execute("UPDATE offer SET state='signed_disputed',reason='late qualification evidence disputes signed allocation' WHERE id=?",(o['id'],))
        db.execute('INSERT INTO allocation_dispute(offer_id,household_id,eligibility_version,state,reason) VALUES(?,?,?,?,?)',
                   (o['id'],household_id,eligibility['version'],'open','signed occupancy retained pending separate legal review'))
        db.execute("UPDATE household SET status='housed_disputed',reason='late qualification evidence after signature' WHERE id=?",(household_id,))
        log(db,'allocation',o['id'],'signed_disputed','late evidence preserved signed unit and opened dispute')
