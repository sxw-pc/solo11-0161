"""Defect, independent inspection, decommission, repair and temporary-placement ledger.

Every state is rebuilt from versioned offline source events. A unit's committable
time window is reconstructed from decommission/restoration events, so no new offer
can be made during an outage and an already-signed lease plus the occupancy it
created can never be rewritten as "never allocated".
"""
from .store import one,many,log

INDEPENDENT={'independent-inspector','housing-audit-office','third-party-surveyor'}

def _require_unit(db,unit_id):
    u=one(db,'SELECT * FROM unit WHERE id=?',(unit_id,))
    if not u:raise ValueError('unknown unit')
    return u

def _active_decommission(db,unit_id):
    return one(db,"SELECT * FROM decommission WHERE unit_id=? AND state='active' ORDER BY decided_at DESC,id DESC LIMIT 1",(unit_id,))

def _open_defect(db,unit_id):
    return one(db,"SELECT * FROM defect WHERE unit_id=? AND state='open' ORDER BY reported_at DESC,id DESC LIMIT 1",(unit_id,))

def _active_placement(db,household_id=None,swing_unit_id=None):
    sql="SELECT * FROM temporary_placement WHERE state='active'"
    args=[]
    if household_id is not None:sql+=' AND household_id=?';args.append(household_id)
    if swing_unit_id is not None:sql+=' AND swing_unit_id=?';args.append(swing_unit_id)
    return one(db,sql+' ORDER BY from_at DESC,id DESC LIMIT 1',tuple(args))

def _next_waiting_household(db,community,rooms,at):
    rows=many(db,"""SELECT q.household_id,q.rank FROM queue_position q JOIN household h ON h.id=q.household_id
                    WHERE q.id=(SELECT MAX(id) FROM queue_position WHERE household_id=q.household_id)
                    AND q.state='waiting' AND h.community=? AND h.status NOT IN ('housed','housed_disputed')
                    ORDER BY q.rank""",(community,))
    for r in rows:
        members=one(db,"SELECT COUNT(*) AS n FROM member WHERE household_id=? AND state='active'",(r['household_id'],))['n']
        if (members+1)//2<=rooms:return r
    return None

def defect_report(db,e):
    u=_require_unit(db,e['unit_id'])
    if not e.get('reporter') or not e.get('summary'):raise ValueError('defect needs reporter and summary')
    db.execute('INSERT INTO defect(id,unit_id,reported_at,reporter,summary,state,reason) VALUES(?,?,?,?,?,?,?)',
               (e['id'],u['id'],e['reported_at'],e['reporter'],e['summary'],'open','defect reported; independent inspection required'))
    log(db,'maintenance',e['id'],'open','water-supply defect reported against '+u['id'],e['id'])
    return 'open'

def inspection(db,e):
    u=_require_unit(db,e['unit_id'])
    d=one(db,'SELECT * FROM defect WHERE id=?',(e['defect_id'],))
    if not d or d['unit_id']!=u['id']:raise ValueError('inspection must reference a defect on the same unit')
    if e['at']<d['reported_at']:raise ValueError('inspection cannot precede the defect report')
    if e.get('inspector') not in INDEPENDENT:raise ValueError('independent inspection requires an independent inspector identity')
    if e['verdict'] not in ('habitable','uninhabitable','long_term_uninhabitable'):raise ValueError('invalid inspection verdict')
    if not e.get('evidence'):raise ValueError('inspection evidence reference required')
    db.execute('INSERT INTO inspection(id,unit_id,defect_id,at,inspector,verdict,evidence,state,reason) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'],u['id'],d['id'],e['at'],e['inspector'],e['verdict'],e['evidence'],'recorded','independent evidence on file'))
    if e['verdict']=='habitable':
        db.execute("UPDATE defect SET state='dismissed',reason='independent inspection found unit habitable' WHERE id=?",(d['id'],))
        log(db,'maintenance',d['id'],'dismissed','independent inspection found no uninhabitable defect',e['id'])
    log(db,'maintenance',e['id'],'recorded','independent inspection verdict '+e['verdict'],e['id'])
    return 'recorded'

def decommission(db,e):
    u=_require_unit(db,e['unit_id'])
    d=one(db,'SELECT * FROM defect WHERE id=?',(e['defect_id'],))
    insp=one(db,'SELECT * FROM inspection WHERE id=?',(e['inspection_id'],))
    if not d or d['unit_id']!=u['id']:raise ValueError('decommission needs the unit defect report')
    if not insp or insp['unit_id']!=u['id'] or insp['defect_id']!=d['id']:raise ValueError('decommission needs matching independent inspection')
    if insp['verdict']=='habitable':raise ValueError('a habitable inspection cannot support decommission')
    if e['effective_from']<d['reported_at']:raise ValueError('outage cannot start before the defect was reported')
    if not e.get('authority'):raise ValueError('decommission decision needs an authority')
    db.execute('INSERT INTO decommission(id,unit_id,defect_id,inspection_id,effective_from,decided_at,authority,prior_state,state,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['id'],u['id'],d['id'],insp['id'],e['effective_from'],e['decided_at'],e['authority'],u['state'],'active','unit out of service pending repair and re-verification'))
    db.execute("UPDATE defect SET state='decommissioned',reason='independent evidence supported outage decision' WHERE id=?",(d['id'],))
    db.execute("UPDATE unit SET state='decommissioned' WHERE id=?",(u['id'],))
    _adjudicate_late_defect(db,u,d,e)
    log(db,'maintenance',e['id'],'active',u['id']+' decommissioned from '+e['effective_from'],e['id'])
    return 'active'

def _adjudicate_late_defect(db,u,d,dec):
    """A defect whose evidence arrives after offers/signatures already exist is ruled
    by the defect's own effective time, not by import order."""
    # Unsigned offers that were open at the defect time are frozen: the mistaken hold
    # is released and the household gets its waiting position back.
    for o in many(db,"SELECT * FROM offer WHERE unit_id=? AND state='offered'",(u['id'],)):
        if o['offered_at']>=d['reported_at']:
            db.execute("UPDATE offer SET state='frozen',reason='late defect evidence predated this unsigned offer; frozen and position restored' WHERE id=?",(o['id'],))
            db.execute("UPDATE unit SET state='decommissioned' WHERE id=?",(u['id'],))
            prior=one(db,'SELECT rank FROM queue_position WHERE household_id=? ORDER BY id DESC LIMIT 1',(o['household_id'],))
            rank=prior['rank'] if prior else 0
            db.execute('INSERT INTO queue_position(household_id,rank,state,reason) VALUES(?,?,?,?)',
                       (o['household_id'],rank,'waiting','restored after late defect froze the mistaken hold'))
            nxt=_next_waiting_household(db,u['community'],u['rooms'],d['reported_at'])
            who=nxt['household_id'] if nxt else 'none'
            log(db,'allocation',o['id'],'frozen','late defect before unsigned offer; next eligible household checked: '+who,dec['id'])
    # Signed occupancy predated by the defect becomes a safety dispute. The signed
    # evidence is retained and the unit never returns to the allocatable pool.
    for o in many(db,"SELECT * FROM offer WHERE unit_id=? AND state='signed'",(u['id'],)):
        sig=one(db,'SELECT * FROM signature WHERE offer_id=? ORDER BY at LIMIT 1',(o['id'],))
        if sig and d['reported_at']<=sig['at'] and not one(db,'SELECT id FROM safety_dispute WHERE offer_id=?',(o['id'],)):
            db.execute('INSERT INTO safety_dispute(offer_id,household_id,unit_id,defect_id,decommission_id,at,state,reason) VALUES(?,?,?,?,?,?,?,?)',
                       (o['id'],o['household_id'],u['id'],d['id'],dec['id'],dec['decided_at'],'open','defect evidence predates signed move-in; occupancy safety disputed and placement duty owed'))
            db.execute("UPDATE household SET status='housed_disputed',reason='safety dispute: defect predates signed occupancy' WHERE id=?",(o['household_id'],))
            log(db,'maintenance',o['id'],'open','safety dispute opened; signed lease evidence retained, unit withheld from pool',dec['id'])

def work_order(db,e):
    u=_require_unit(db,e['unit_id'])
    dec=_active_decommission(db,u['id'])
    if not dec:raise ValueError('work order requires an active decommission')
    if not e.get('crew') or not e.get('required_by'):raise ValueError('work order needs repair crew and required-by time')
    db.execute('INSERT INTO work_order(id,unit_id,decommission_id,opened_at,crew,required_by,state,reason) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'],u['id'],dec['id'],e['opened_at'],e['crew'],e['required_by'],'open','repair work opened; completion self-report is not independent evidence'))
    log(db,'maintenance',e['id'],'open','repair work order opened for '+u['id'],e['id'])
    return 'open'

def repair_complete(db,e):
    w=one(db,'SELECT * FROM work_order WHERE id=?',(e['work_order_id'],))
    if not w:raise ValueError('unknown work order')
    if w['state']!='open':raise ValueError('work order already closed')
    if e['at']<w['opened_at']:raise ValueError('completion cannot precede work order opening')
    db.execute("UPDATE work_order SET completed_at=?,state='self_reported',reason='repair crew self-report; independent re-inspection required before return' WHERE id=?",
               (e['at'],w['id']))
    log(db,'maintenance',w['id'],'self_reported','crew reported completion; unit stays decommissioned pending re-inspection',e['id'])
    return 'self_reported'

def reinspect(db,e):
    w=one(db,'SELECT * FROM work_order WHERE id=?',(e['work_order_id'],))
    if not w:raise ValueError('unknown work order')
    u=_require_unit(db,w['unit_id'])
    if e.get('inspector') not in INDEPENDENT:raise ValueError('re-inspection requires an independent inspector identity')
    if not e.get('evidence'):raise ValueError('re-inspection evidence reference required')
    if e['at']<w['opened_at']:raise ValueError('re-inspection cannot precede repair work')
    passed=1 if e['passed'] else 0
    state='passed' if passed else 'failed'
    db.execute('INSERT INTO reinspection(work_order_id,unit_id,at,inspector,passed,evidence,state,reason) VALUES(?,?,?,?,?,?,?,?)',
               (w['id'],u['id'],e['at'],e['inspector'],passed,e['evidence'],state,'independent re-verification of repaired unit'))
    if not passed:
        # Self-reported completion conflicts with independent evidence: the outage
        # continues and an accountability record is kept.
        db.execute("UPDATE work_order SET state='disputed',reason='independent re-inspection failed despite crew completion self-report' WHERE id=?",(w['id'],))
        db.execute('INSERT INTO accountability(unit_id,work_order_id,reinspection_id,at,state,reason) VALUES(?,?,?,?,?,?)',
                   (u['id'],w['id'],e['id'],e['at'],'open','repair crew completion self-report contradicted by independent evidence; unit remains decommissioned'))
        log(db,'maintenance',w['id'],'disputed','completion self-report contradicted by independent re-inspection; outage continues',e['id'])
    else:
        db.execute("UPDATE work_order SET state='verified',reason='independent re-inspection passed' WHERE id=?",(w['id'],))
        log(db,'maintenance',w['id'],'verified','independent re-inspection passed; unit eligible for return-to-service',e['id'])
    return state

def return_to_service(db,e):
    u=_require_unit(db,e['unit_id'])
    dec=_active_decommission(db,u['id'])
    if not dec:raise ValueError('unit has no active decommission')
    passed=one(db,"SELECT * FROM reinspection WHERE unit_id=? AND passed=1 ORDER BY at DESC,id DESC LIMIT 1",(u['id'],))
    if not passed:raise ValueError('return to service requires a passed independent re-inspection')
    if e['at']<passed['at']:raise ValueError('restoration cannot precede the passed re-inspection')
    db.execute("UPDATE decommission SET restored_at=?,state='restored',reason='returned to service after passed independent re-inspection' WHERE id=?",
               (e['at'],dec['id']))
    # A unit that was assigned before the outage keeps its original lease; it is not
    # pooled for new offers. Only previously free stock becomes available again.
    new_state='assigned' if dec['prior_state']=='assigned' else 'available'
    db.execute('UPDATE unit SET state=? WHERE id=?',(new_state,u['id']))
    log(db,'maintenance',dec['id'],'restored',u['id']+' returned to service as '+new_state,e['id'])
    return 'restored'

def temporary_placement(db,e):
    h=one(db,'SELECT * FROM household WHERE id=?',(e['household_id'],))
    swing=_require_unit(db,e['swing_unit_id'])
    original=one(db,'SELECT * FROM unit WHERE id=?',(e['original_unit_id'],))
    if not h:raise ValueError('unknown household')
    if not original:raise ValueError('unknown original unit')
    if swing['usage']!='swing':raise ValueError('temporary placement requires a compliant swing unit')
    if not _active_decommission(db,original['id']) and not one(db,'SELECT id FROM safety_dispute WHERE unit_id=? AND state=?',(original['id'],'open')):
        raise ValueError('temporary placement requires an active outage or open safety dispute on the original unit')
    if swing['state']!='available':raise ValueError('swing unit has no free capacity')
    if _active_placement(db,household_id=h['id']):raise ValueError('household already has an active temporary placement')
    db.execute('INSERT INTO temporary_placement(id,household_id,swing_unit_id,original_unit_id,from_at,planned_to_at,state,reason) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'],h['id'],swing['id'],original['id'],e['from_at'],e.get('planned_to_at'),'active','short-term safety placement consumes swing capacity only'))
    # The swing unit occupies real capacity, but this never converts the household
    # into a new applicant: the original queue/lease position is untouched.
    db.execute("UPDATE unit SET state='held' WHERE id=?",(swing['id'],))
    log_placement_duty(db,h['id'])
    log(db,'maintenance',e['id'],'active',h['id']+' placed in swing unit '+swing['id']+' without altering original queue standing',e['id'])
    return 'active'

def reoccupy(db,e):
    p=one(db,'SELECT * FROM temporary_placement WHERE id=?',(e['placement_id'],))
    if not p:raise ValueError('unknown temporary placement')
    if p['state']!='active':raise ValueError('placement already ended')
    u=_require_unit(db,p['original_unit_id'])
    latest=one(db,"SELECT * FROM decommission WHERE unit_id=? ORDER BY decided_at DESC,id DESC LIMIT 1",(u['id'],))
    if not latest or latest['state']!='restored':raise ValueError('original unit must be restored after a passed independent re-inspection')
    passed=one(db,"SELECT * FROM reinspection WHERE unit_id=? AND passed=1 ORDER BY at DESC,id DESC LIMIT 1",(u['id'],))
    if not passed or e['at']<passed['at']:raise ValueError('move-back requires a passed independent re-inspection first')
    db.execute("UPDATE temporary_placement SET ended_at=?,state='ended',reason='household moved back after successful re-verification' WHERE id=?",
               (e['at'],p['id']))
    db.execute("UPDATE unit SET state='available' WHERE id=?",(p['swing_unit_id'],))
    db.execute('INSERT INTO reoccupation(id,placement_id,household_id,unit_id,at,state,reason) VALUES(?,?,?,?,?,?,?)',
               (e['id'],p['id'],p['household_id'],u['id'],e['at'],'completed','return-to-original handover after repair'))
    log_placement_duty(db,p['household_id'])
    log(db,'maintenance',p['id'],'ended',p['household_id']+' reoccupied original unit '+u['id'],e['id'])
    return 'completed'

def replacement_pledge(db,e):
    h=one(db,'SELECT * FROM household WHERE id=?',(e['household_id'],))
    original=_require_unit(db,e['original_unit_id'])
    repl=_require_unit(db,e['replacement_unit_id'])
    insp=one(db,'SELECT * FROM inspection WHERE id=?',(e['inspection_id'],))
    if not h:raise ValueError('unknown household')
    if not insp or insp['unit_id']!=original['id'] or insp['verdict']!='long_term_uninhabitable':
        raise ValueError('permanent replacement needs a long-term-uninhabitable independent inspection')
    if not e.get('consent_ref'):raise ValueError('written household consent reference required')
    if repl['usage']!='normal':raise ValueError('replacement must be a normal allocatable unit')
    if repl['state']!='available':raise ValueError('replacement unit is not free')
    eligibility=one(db,'SELECT * FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(h['id'],))
    if not eligibility or eligibility['state']!='qualified':raise ValueError('current replacement qualification absent or not qualified')
    if one(db,"SELECT id FROM permanent_replacement WHERE household_id=? AND completed_at IS NULL",(h['id'],)):
        raise ValueError('household already has an open permanent replacement')
    db.execute('INSERT INTO permanent_replacement(id,household_id,original_unit_id,replacement_unit_id,inspection_id,consent_ref,pledged_at,eligibility_version,state,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['id'],h['id'],original['id'],repl['id'],insp['id'],e['consent_ref'],e['pledged_at'],eligibility['version'],'pledged','permanent replacement promised pending two-party handover'))
    db.execute("UPDATE unit SET state='held' WHERE id=?",(repl['id'],))
    log_placement_duty(db,h['id'])
    log(db,'maintenance',e['id'],'pledged',h['id']+' promised permanent replacement '+repl['id']+' at eligibility version '+str(eligibility['version']),e['id'])
    return 'pledged'

def replacement_handover(db,e):
    p=one(db,'SELECT * FROM permanent_replacement WHERE id=?',(e['pledge_id'],))
    if not p:raise ValueError('unknown replacement pledge')
    if p['state'] not in ('pledged','reconfirmed'):raise ValueError('pledge is not open for handover')
    if not e.get('handover_ref'):raise ValueError('two-party handover record reference required')
    eligibility=one(db,'SELECT * FROM eligibility WHERE household_id=? ORDER BY id DESC LIMIT 1',(p['household_id'],))
    if not eligibility or eligibility['state']!='qualified':raise ValueError('handover requires current qualified replacement status')
    # Any active swing stay ends with the handover; its short tenancy never consumed
    # the original signed lease.
    placement=_active_placement(db,household_id=p['household_id'])
    if placement:
        db.execute("UPDATE temporary_placement SET ended_at=?,state='ended',reason='ended by permanent replacement handover' WHERE id=?",
                   (e['at'],placement['id']))
        db.execute("UPDATE unit SET state='available' WHERE id=?",(placement['swing_unit_id'],))
    # The original signed lease remains evidence and the defective unit stays out of
    # the pool; the replacement unit is what gets delivered.
    db.execute("UPDATE unit SET state='assigned' WHERE id=?",(p['replacement_unit_id'],))
    db.execute("UPDATE permanent_replacement SET completed_at=?,state='completed',reason='two-party handover delivered permanent replacement' WHERE id=?",
               (e['at'],p['id']))
    log_placement_duty(db,p['household_id'])
    log(db,'maintenance',p['id'],'completed','permanent replacement handed over; original defective unit retained out of pool',e['id'])
    return 'completed'

def log_placement_duty(db,household_id):
    """Maintain one open/closed safety-placement duty row per household so the duty
    survives later eligibility versions."""
    active=one(db,"SELECT id FROM temporary_placement WHERE household_id=? AND state='active'",(household_id,))
    open_dispute=one(db,"SELECT id FROM safety_dispute WHERE household_id=? AND state='open'",(household_id,))
    open_replacement=one(db,"SELECT id FROM permanent_replacement WHERE household_id=? AND completed_at IS NULL",(household_id,))
    reason='temporary safety placement duty retained'
    if active:
        state='duty_active'
    elif open_dispute or open_replacement:
        state='duty_pending'
    else:
        state='duty_closed'
        reason='placement and replacement obligations concluded'
    log(db,'maintenance','placement-duty:'+household_id,state,reason)
