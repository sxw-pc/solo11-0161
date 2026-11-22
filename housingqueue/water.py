from .store import one, log

TAP_THRESHOLD = 100


def occupant(db, unit_id):
    """Return the household actually occupying a unit, or None for a vacant unit.

    A signed allocation remains an occupancy fact even when the qualification is
    later disputed (allocation.reconcile_offers keeps the signed unit and opens
    a dispute), so both 'housed' and 'housed_disputed' households are reached.
    """
    if not unit_id:
        return None
    row = one(
        db,
        "SELECT o.household_id FROM offer o "
        "JOIN household h ON h.id=o.household_id "
        "WHERE o.unit_id=? AND o.state IN ('signed','signed_disputed') "
        "AND h.status IN ('housed','housed_disputed') ORDER BY o.rowid DESC LIMIT 1",
        (unit_id,))
    return row['household_id'] if row else None


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
    state = 'alert' if count >= TAP_THRESHOLD else 'clear'
    db.execute('INSERT INTO water_sample(id,tap_id,sampled_at,cfu_per_litre,state) VALUES(?,?,?,?,?)',
               (e['id'], e['tap_id'], e['sampled_at'], count, state))
    log(db, 'water', e['id'], state, 'individual tap threshold', e['id'])
    return state
