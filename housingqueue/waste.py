from .store import one, log
from .reward import decide as reward_decide


def decide(db, household_id, event_id=None):
    grams = one(db, 'SELECT COALESCE(SUM(mass_grams),0) AS n FROM waste_drop WHERE household_id=?', (household_id,))['n']
    state = 'accepted' if grams else 'pending'
    db.execute('INSERT INTO waste_decision(household_id,accepted_grams,state) VALUES(?,?,?) ON CONFLICT(household_id) DO UPDATE SET accepted_grams=excluded.accepted_grams,state=excluded.state',
               (household_id, grams, state))
    log(db, 'waste', household_id, state, 'gross deposited material weighed', event_id)
    reward_decide(db, household_id, event_id)
    return state


def drop(db, e):
    h = one(db, 'SELECT * FROM household WHERE id=?', (e['household_id'],))
    if not h or h['status'] not in ('housed', 'housed_disputed'):
        raise ValueError('waste deposit requires housed household')
    if e['stream'] not in ('paper', 'plastic', 'metal') or int(e['mass_grams']) <= 0:
        raise ValueError('known stream and positive mass required')
    db.execute('INSERT INTO waste_drop(id,household_id,stream,mass_grams,at) VALUES(?,?,?,?,?)',
               (e['id'], e['household_id'], e['stream'], int(e['mass_grams']), e['at']))
    return decide(db, e['household_id'], e['id'])
