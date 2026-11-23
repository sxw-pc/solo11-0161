from .store import one
from . import dispatch


def decide(db, community, event_id=None):
    """Sum the latest meter reading of every physically delivered occupied unit.

    Signed-but-undelivered units are not counted as occupied loads. A community
    without any reading stays 'pending': missing measurements are never zero.
    """
    return dispatch.recompute_demand(db, community, event_id)


def reading(db, e):
    unit = one(db, 'SELECT id,community FROM unit WHERE id=?', (e['unit_id'],))
    if not unit or int(e['kw_tenths']) < 0:
        raise ValueError('known unit and nonnegative measured demand required')
    db.execute('INSERT INTO power_reading(id,unit_id,at,kw_tenths,meter) VALUES(?,?,?,?,?)',
               (e['id'], e['unit_id'], e['at'], int(e['kw_tenths']), e['meter']))
    state = decide(db, unit['community'], e['id'])
    # later meter data may only touch segments that have not executed yet
    dispatch.refresh_after_meter(db, unit['id'], e['at'], e['id'])
    return state
