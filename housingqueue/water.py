from .store import one, log


def tap(db, e):
    u = one(db, 'SELECT id FROM unit WHERE id=?', (e['unit_id'],))
    if not u:
        raise ValueError('tap requires a known unit')
    db.execute('INSERT INTO water_tap(id,unit_id,zone) VALUES(?,?,?)',
               (e['id'], u['id'], e['zone']))
    log(db, 'water', e['id'], 'registered', 'unit tap registered', e['id'])
    return 'registered'


def sample(db, e):
    tap = one(db, 'SELECT * FROM water_tap WHERE id=?', (e['tap_id'],))
    count = int(e['cfu_per_litre'])
    if not tap or count < 0:
        raise ValueError('known tap and nonnegative sample result required')
    state = 'alert' if count >= 100 else 'clear'
    db.execute('INSERT INTO water_sample(id,tap_id,sampled_at,cfu_per_litre,state) VALUES(?,?,?,?,?)',
               (e['id'], e['tap_id'], e['sampled_at'], count, state))
    log(db, 'water', e['id'], state, 'individual tap threshold', e['id'])
    return state
