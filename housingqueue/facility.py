import json

from .store import one, many, log

NOISE = {'quiet': 0, 'normal': 1, 'loud': 2}


def room(db, e):
    if int(e['capacity']) <= 0:
        raise ValueError('facility capacity must be positive')
    noise = e.get('noise', 'normal')
    if noise not in NOISE:
        raise ValueError('unknown room noise ceiling')
    db.execute('INSERT INTO facility_room(id,community,capacity,accessible,noise,equipment,dividable) VALUES(?,?,?,?,?,?,?)',
               (e['id'], e['community'], int(e['capacity']), int(bool(e.get('accessible'))), noise,
                json.dumps(sorted(e.get('equipment', []))), int(bool(e.get('dividable')))))
    for a in e.get('areas', []):
        if int(a['capacity']) <= 0:
            raise ValueError('area capacity must be positive')
        a_noise = a.get('noise', noise)
        if a_noise not in NOISE:
            raise ValueError('unknown area noise ceiling')
        db.execute('INSERT INTO facility_area(id,room_id,capacity,accessible,noise,equipment) VALUES(?,?,?,?,?,?)',
                   (a['id'], e['id'], int(a['capacity']), int(bool(a.get('accessible'))), a_noise,
                    json.dumps(sorted(a.get('equipment', [])))))
    log(db, 'facility', e['id'], 'open', 'community room registered', e['id'])
    return 'open'


def booking(db, e):
    household = one(db, 'SELECT * FROM household WHERE id=?', (e['household_id'],))
    room = one(db, 'SELECT * FROM facility_room WHERE id=?', (e['room_id'],))
    if not household or not room or e['starts'] >= e['ends'] or int(e['attendees']) <= 0:
        raise ValueError('known household and room, valid booking interval required')
    if household['status'] not in ('housed', 'housed_disputed') or household['community'] != room['community']:
        state, reason = 'blocked', 'household not housed in room community'
    elif int(e['attendees']) > room['capacity']:
        state, reason = 'blocked', 'room capacity too small'
    elif one(db, "SELECT 1 FROM facility_booking b JOIN facility_decision d ON d.booking_id=b.id WHERE b.room_id=? AND d.state='reserved' AND b.starts<? AND b.ends>?", (room['id'], e['ends'], e['starts'])):
        state, reason = 'conflict', 'room already reserved for overlapping interval'
    elif one(db, "SELECT 1 FROM facility_room_use u WHERE u.room_id=? AND u.state='occupied' AND u.starts<? AND u.ends>?", (room['id'], e['ends'], e['starts'])):
        state, reason = 'conflict', 'room occupied by scheduled activity session'
    else:
        state, reason = 'reserved', 'housed resident reserved compatible room'
    db.execute('INSERT INTO facility_booking(id,household_id,room_id,starts,ends,attendees) VALUES(?,?,?,?,?,?)',
               (e['id'], e['household_id'], e['room_id'], e['starts'], e['ends'], int(e['attendees'])))
    db.execute('INSERT INTO facility_decision(booking_id,state,reason) VALUES(?,?,?)',
               (e['id'], state, reason))
    log(db, 'facility', e['id'], state, reason, e['id'])
    return state


def activity(db, e):
    h = one(db, 'SELECT * FROM household WHERE id=?', (e['household_id'],))
    if not h:
        raise ValueError('unknown household')
    if e['starts'] >= e['ends'] or int(e['attendees']) <= 0:
        raise ValueError('valid activity window and attendee goal required')
    noise = e.get('noise', 'normal')
    if noise not in NOISE:
        raise ValueError('unknown activity noise level')
    if h['status'] not in ('housed', 'housed_disputed'):
        state, reason = 'blocked', 'host household not housed in community'
    else:
        state, reason = 'authorized', 'host household housed; joint attendance goal recorded'
    db.execute('INSERT INTO activity(id,household_id,community,title,attendees,starts,ends,noise,equipment,accessible,state,reason,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (e['id'], h['id'], h['community'], e.get('title', ''), int(e['attendees']), e['starts'], e['ends'], noise,
                json.dumps(sorted(e.get('equipment', []))), int(bool(e.get('accessible'))), state, reason, 'pending'))
    log(db, 'facility', e['id'], state, reason, e['id'])
    return state


def consent(db, e):
    act = one(db, 'SELECT * FROM activity WHERE id=?', (e['activity_id'],))
    if not act:
        raise ValueError('unknown activity')
    applicant = one(db, "SELECT id FROM member WHERE household_id=? AND relation='applicant' AND state='active'", (act['household_id'],))
    if not applicant or e.get('signer') != applicant['id']:
        log(db, 'facility', act['id'], 'consent_rejected', 'split consent requires host household applicant signature', e['id'])
        return 'rejected'
    windows = e.get('windows') or []
    if not windows:
        raise ValueError('consent requires at least one split window')
    for i, w in enumerate(windows):
        if w['starts'] >= w['ends'] or int(w['attendees']) <= 0:
            raise ValueError('valid consent window required')
        db.execute('INSERT INTO activity_window(id,activity_id,starts,ends,attendees,consented_at,source) VALUES(?,?,?,?,?,?,?)',
                   (e['id'] + ':' + str(i), act['id'], w['starts'], w['ends'], int(w['attendees']), e['at'], e['id']))
    log(db, 'facility', act['id'], 'consented', 'host household consented split windows in writing', e['id'])
    return 'consented'


def window_cap(db, act, starts, ends):
    if starts == act['starts'] and ends == act['ends']:
        return int(act['attendees']), 'primary'
    w = one(db, 'SELECT * FROM activity_window WHERE activity_id=? AND starts<=? AND ends>=? AND consented_at<=? ORDER BY rowid LIMIT 1',
            (act['id'], starts, ends, starts))
    if w:
        return w['attendees'], 'split'
    return None, None


def assess_session(db, act, room, area, starts, ends, attendees, exclude_session=''):
    if act['state'] != 'authorized':
        return 'blocked', 'activity not authorized'
    cap, kind = window_cap(db, act, starts, ends)
    if cap is None:
        return 'blocked', 'outside primary goal window and consented split windows'
    if attendees > cap:
        return 'blocked', 'consented window attendance limit exceeded'
    if room['community'] != act['community']:
        return 'blocked', 'room outside activity community'
    space = area if area else room
    if attendees > space['capacity']:
        return 'blocked', 'room capacity too small'
    if act['accessible'] and not space['accessible']:
        return 'blocked', 'accessible room required'
    if NOISE[act['noise']] > NOISE[space['noise']]:
        return 'blocked', 'activity noise above room ceiling'
    needs = sorted(json.loads(act['equipment']))
    have = set(json.loads(space['equipment']))
    missing = [x for x in needs if x not in have]
    if missing:
        return 'blocked', 'room equipment missing: ' + ','.join(missing)
    area_id = area['id'] if area else None
    if one(db, "SELECT u.session_id FROM facility_room_use u WHERE u.room_id=? AND u.state='occupied' AND u.starts<? AND u.ends>? AND (u.area_id IS NULL OR ? IS NULL OR u.area_id=?) AND u.session_id<>?",
           (room['id'], ends, starts, area_id, area_id, exclude_session)):
        return 'conflict', 'room already occupied for overlapping interval'
    if one(db, "SELECT 1 FROM facility_booking b JOIN facility_decision d ON d.booking_id=b.id WHERE b.room_id=? AND d.state='reserved' AND b.starts<? AND b.ends>?",
           (room['id'], ends, starts)):
        return 'conflict', 'room reserved by single booking for overlapping interval'
    return 'scheduled', kind + ' window within consented time, capacity, access, noise and equipment conditions'


def session(db, e):
    act = one(db, 'SELECT * FROM activity WHERE id=?', (e['activity_id'],))
    room = one(db, 'SELECT * FROM facility_room WHERE id=?', (e['room_id'],))
    if not act or not room:
        raise ValueError('known activity and room required')
    if e['starts'] >= e['ends'] or int(e['attendees']) <= 0:
        raise ValueError('valid session interval and attendees required')
    area = None
    if e.get('area_id'):
        area = one(db, 'SELECT * FROM facility_area WHERE id=? AND room_id=?', (e['area_id'], room['id']))
        if not area:
            raise ValueError('unknown room area')
    state, reason = assess_session(db, act, room, area, e['starts'], e['ends'], int(e['attendees']))
    needs = json.loads(act['equipment'])
    have = json.loads(area['equipment']) if area else json.loads(room['equipment'])
    db.execute('INSERT INTO facility_session(id,activity_id,room_id,area_id,starts,ends,attendees,state,reason,needs_equipment,room_equipment) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (e['id'], act['id'], room['id'], area['id'] if area else None, e['starts'], e['ends'],
                int(e['attendees']), state, reason, json.dumps(needs), json.dumps(have)))
    if state == 'scheduled':
        db.execute('INSERT INTO facility_room_use(session_id,room_id,area_id,starts,ends,state) VALUES(?,?,?,?,?,?)',
                   (e['id'], room['id'], area['id'] if area else None, e['starts'], e['ends'], 'occupied'))
    log(db, 'facility', e['id'], state, reason, e['id'])
    recompute_activity(db, act['id'], e['id'])
    return state


def session_move(db, e):
    s = one(db, 'SELECT * FROM facility_session WHERE id=?', (e['session_id'],))
    if not s:
        raise ValueError('unknown session')
    room = one(db, 'SELECT * FROM facility_room WHERE id=?', (e['room_id'],))
    if not room:
        raise ValueError('unknown room')
    area = None
    if e.get('area_id'):
        area = one(db, 'SELECT * FROM facility_area WHERE id=? AND room_id=?', (e['area_id'], room['id']))
        if not area:
            raise ValueError('unknown room area')
    if s['state'] != 'scheduled':
        state, reason = 'invalid', 'only a scheduled session may move rooms'
    else:
        act = one(db, 'SELECT * FROM activity WHERE id=?', (s['activity_id'],))
        actual = one(db, "SELECT COUNT(*) AS n FROM facility_checkin WHERE session_id=? AND state='valid'", (s['id'],))['n']
        st, rs = assess_session(db, act, room, area, s['starts'], s['ends'], max(s['attendees'], actual), exclude_session=s['id'])
        if st != 'scheduled':
            state, reason = 'blocked', 'room change rejected: ' + rs
        else:
            have = json.loads(area['equipment']) if area else json.loads(room['equipment'])
            db.execute("UPDATE facility_room_use SET state='released',released_at=? WHERE session_id=? AND state='occupied'", (e['at'], s['id']))
            db.execute('INSERT INTO facility_room_use(session_id,room_id,area_id,starts,ends,state) VALUES(?,?,?,?,?,?)',
                       (s['id'], room['id'], area['id'] if area else None, s['starts'], s['ends'], 'occupied'))
            db.execute('UPDATE facility_session SET room_id=?,area_id=?,room_equipment=?,reason=? WHERE id=?',
                       (room['id'], area['id'] if area else None, json.dumps(have),
                        'moved rooms; equipment and actual attendees rechecked', s['id']))
            state, reason = 'moved', 'session moved rooms; equipment and actual attendees satisfy new room'
        recompute_activity(db, s['activity_id'], e['id'])
    log(db, 'facility', s['id'], state, reason, e['id'])
    return state


def session_cancel(db, e):
    s = one(db, 'SELECT * FROM facility_session WHERE id=?', (e['session_id'],))
    if not s:
        raise ValueError('unknown session')
    held = one(db, "SELECT 1 FROM facility_checkin WHERE session_id=? AND state='valid'", (s['id'],))
    if s['state'] != 'scheduled' or held:
        state, reason = 'invalid', 'only a scheduled session without check-ins may cancel'
    else:
        db.execute("UPDATE facility_session SET state='cancelled',reason=? WHERE id=?",
                   ('cancelled by host: ' + e.get('reason', ''), s['id']))
        db.execute("UPDATE facility_room_use SET state='released',released_at=? WHERE session_id=? AND state='occupied'", (e['at'], s['id']))
        state, reason = 'cancelled', 'session cancelled before holding; room released'
        recompute_activity(db, s['activity_id'], e['id'])
    log(db, 'facility', s['id'], state, reason, e['id'])
    return state


def guest_pass(db, e):
    act = one(db, 'SELECT * FROM activity WHERE id=?', (e['activity_id'],))
    if not act:
        raise ValueError('unknown activity')
    applicant = one(db, "SELECT id FROM member WHERE household_id=? AND relation='applicant' AND state='active'", (act['household_id'],))
    if not applicant or e.get('issuer') != applicant['id']:
        state, reason = 'rejected', 'guest authorization requires host household applicant'
    elif one(db, "SELECT 1 FROM member WHERE id=? AND household_id=? AND state='active'", (e['person_id'], act['household_id'])):
        state, reason = 'rejected', 'household member needs no guest pass'
    else:
        state, reason = 'authorized', 'host authorized external guest for the activity sessions'
    db.execute('INSERT INTO guest_pass(id,activity_id,person_id,issuer,at,state,reason) VALUES(?,?,?,?,?,?,?)',
               (e['id'], act['id'], e['person_id'], e['issuer'], e['at'], state, reason))
    log(db, 'facility', act['id'], 'guest_' + state, e['person_id'] + ': ' + reason, e['id'])
    return state


def witness_ok(db, act, e):
    witness = e.get('witness')
    kind = e.get('witness_kind')
    if not witness or witness == e['person_id'] or kind not in ('host', 'independent'):
        return False
    if kind == 'host':
        return bool(one(db, "SELECT 1 FROM member WHERE id=? AND household_id=? AND relation='applicant' AND state='active'",
                          (witness, act['household_id'])))
    return not one(db, "SELECT 1 FROM member WHERE id=? AND household_id=? AND state='active'",
                   (witness, act['household_id']))


def identity_source(db, act, s, e):
    if one(db, "SELECT 1 FROM member WHERE id=? AND household_id=? AND state='active' AND joined_at<=?",
           (e['person_id'], act['household_id'], s['starts'])):
        return 'member'
    if one(db, "SELECT 1 FROM guest_pass WHERE activity_id=? AND person_id=? AND state='authorized' AND at<=?",
           (act['id'], e['person_id'], e['at'])):
        return 'guest'
    return None


def checkin(db, e):
    s = one(db, 'SELECT * FROM facility_session WHERE id=?', (e['session_id'],))
    if not s:
        raise ValueError('unknown session')
    act = one(db, 'SELECT * FROM activity WHERE id=?', (s['activity_id'],))
    room = one(db, 'SELECT * FROM facility_room WHERE id=?', (s['room_id'],))
    area = one(db, 'SELECT * FROM facility_area WHERE id=? AND room_id=?', (s['area_id'], room['id'])) if s['area_id'] else None
    source, late = None, 0
    if s['state'] != 'scheduled':
        state, reason = 'rejected', 'session not open for check-in'
    elif not (s['starts'] <= e['at'] <= s['ends']):
        state, reason = 'rejected', 'check-in outside session window'
    elif not witness_ok(db, act, e):
        state, reason = 'rejected', 'host or independent witness required'
    else:
        source = identity_source(db, act, s, e)
        if not source:
            state, reason = 'void', 'no effective membership or guest authorization'
        elif one(db, "SELECT 1 FROM facility_checkin c JOIN facility_session x ON x.id=c.session_id WHERE c.person_id=? AND c.state='valid' AND x.starts<? AND x.ends>? AND x.id<>?",
                 (e['person_id'], s['ends'], s['starts'], s['id'])):
            state, reason = 'rejected', 'person already checked into overlapping session'
        else:
            cap, _ = window_cap(db, act, s['starts'], s['ends'])
            cap = min(cap, (area if area else room)['capacity'])
            n = one(db, "SELECT COUNT(*) AS n FROM facility_checkin WHERE session_id=? AND state='valid'", (s['id'],))['n']
            if n >= cap:
                state, reason = 'rejected', 'session full under consented window and room capacity'
            else:
                state, reason = 'valid', source + ' identity verified with witness'
                late = int(e['at'] > s['starts'])
    db.execute('INSERT INTO facility_checkin(id,session_id,person_id,at,witness,witness_kind,source,late,state,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['id'], s['id'], e['person_id'], e['at'], e.get('witness', ''), e.get('witness_kind', ''), source, late, state, reason))
    log(db, 'facility', s['id'], 'checkin_' + state, e['person_id'] + ': ' + reason, e['id'])
    if state == 'valid':
        recompute_activity(db, act['id'], e['id'])
    return state


def attendee_correction(db, e):
    act = one(db, 'SELECT * FROM activity WHERE id=?', (e['activity_id'],))
    if not act or not e.get('reviewer') or e['reviewer'] == e['person_id']:
        raise ValueError('known activity and independent reviewer required')
    apply_person_correction(db, e['person_id'], act['household_id'], e['at'], e['id'], activity_id=act['id'], members_only=False)
    log(db, 'facility', act['id'], 'attendee_corrected', e['person_id'] + ': ' + e.get('reason', ''), e['id'])
    return 'corrected'


def apply_person_correction(db, person_id, household_id, at, event_id, activity_id=None, members_only=True):
    if activity_id:
        activity_ids = [activity_id]
    else:
        activity_ids = [a['id'] for a in many(db, 'SELECT id FROM activity WHERE household_id=?', (household_id,))]
    for aid in activity_ids:
        touched = False
        for s in many(db, 'SELECT * FROM facility_session WHERE activity_id=?', (aid,)):
            if members_only:
                cur = db.execute("UPDATE facility_checkin SET state='void',reason='membership withdrawn by correction' WHERE session_id=? AND person_id=? AND state='valid' AND source='member'",
                                 (s['id'], person_id))
            else:
                cur = db.execute("UPDATE facility_checkin SET state='void',reason='attendee authorization withdrawn by correction' WHERE session_id=? AND person_id=? AND state='valid'",
                                 (s['id'], person_id))
            if cur.rowcount:
                touched = True
                if s['state'] == 'scheduled' and s['ends'] > at:
                    db.execute("UPDATE facility_session SET state='clearance_withdrawn',reason='incomplete clearance withdrawn after attendee correction' WHERE id=?", (s['id'],))
                    db.execute("UPDATE facility_room_use SET state='released',released_at=? WHERE session_id=? AND state='occupied'", (at, s['id']))
                    log(db, 'facility', s['id'], 'clearance_withdrawn', 'attendee correction withdrew incomplete room clearance', event_id)
        if touched:
            recompute_activity(db, aid, event_id)


def recompute_activity(db, activity_id, event_id=None):
    act = one(db, 'SELECT * FROM activity WHERE id=?', (activity_id,))
    if not act:
        return
    joint_session, split_held, open_any = None, False, False
    for s in many(db, 'SELECT * FROM facility_session WHERE activity_id=? ORDER BY rowid', (activity_id,)):
        if s['state'] != 'scheduled':
            continue
        n = one(db, "SELECT COUNT(*) AS n FROM facility_checkin WHERE session_id=? AND state='valid'", (s['id'],))['n']
        primary = s['starts'] == act['starts'] and s['ends'] == act['ends']
        if primary and n >= act['attendees']:
            joint_session = s['id']
        elif not primary and n > 0:
            split_held = True
        else:
            open_any = True
    if joint_session:
        outcome, outcome_session = 'joint_achieved', joint_session
    elif split_held:
        outcome, outcome_session = 'split_held', None
    elif open_any:
        outcome, outcome_session = 'scheduled', None
    else:
        outcome, outcome_session = 'pending', None
    if (act['outcome'], act['outcome_session_id']) != (outcome, outcome_session):
        db.execute('UPDATE activity SET outcome=?,outcome_session_id=? WHERE id=?', (outcome, outcome_session, activity_id))
        log(db, 'facility', activity_id, 'outcome_' + outcome, 'activity goal outcome recomputed', event_id)
