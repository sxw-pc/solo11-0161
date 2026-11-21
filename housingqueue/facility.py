from .store import one, log


def room(db, e):
    if int(e['capacity']) <= 0:
        raise ValueError('facility capacity must be positive')
    db.execute('INSERT INTO facility_room(id,community,capacity,accessible) VALUES(?,?,?,?)',
               (e['id'], e['community'], int(e['capacity']), int(bool(e.get('accessible')))))
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
    else:
        state, reason = 'reserved', 'housed resident reserved compatible room'
    db.execute('INSERT INTO facility_booking(id,household_id,room_id,starts,ends,attendees) VALUES(?,?,?,?,?,?)',
               (e['id'], e['household_id'], e['room_id'], e['starts'], e['ends'], int(e['attendees'])))
    db.execute('INSERT INTO facility_decision(booking_id,state,reason) VALUES(?,?,?)',
               (e['id'], state, reason))
    log(db, 'facility', e['id'], state, reason, e['id'])
    return state
