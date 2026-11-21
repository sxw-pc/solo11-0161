from .store import one, log


def decide(db, community, event_id=None):
    units = db.execute("SELECT id FROM unit WHERE community=? AND state='assigned'", (community,)).fetchall()
    total = count = 0
    for unit in units:
        latest = one(db, 'SELECT kw_tenths FROM power_reading WHERE unit_id=? ORDER BY at DESC,id DESC LIMIT 1', (unit['id'],))
        if latest:
            total += latest['kw_tenths']
            count += 1
    state = 'measured' if count else 'pending'
    db.execute('INSERT INTO power_demand(community,total_kw_tenths,reading_count,state) VALUES(?,?,?,?) ON CONFLICT(community) DO UPDATE SET total_kw_tenths=excluded.total_kw_tenths,reading_count=excluded.reading_count,state=excluded.state',
               (community, total, count, state))
    log(db, 'power', community, state, 'latest occupied-unit meter readings summed', event_id)
    return state


def reading(db, e):
    unit = one(db, 'SELECT id,community FROM unit WHERE id=?', (e['unit_id'],))
    if not unit or int(e['kw_tenths']) < 0:
        raise ValueError('known unit and nonnegative measured demand required')
    db.execute('INSERT INTO power_reading(id,unit_id,at,kw_tenths,meter) VALUES(?,?,?,?,?)',
               (e['id'], e['unit_id'], e['at'], int(e['kw_tenths']), e['meter']))
    return decide(db, unit['community'], e['id'])
