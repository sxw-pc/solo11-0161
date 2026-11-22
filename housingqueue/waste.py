from .store import one, log
from .reward import decide as reward_decide


def decide(db, household_id, event_id=None):
    # 入站毛重只作受理记录，永不被后续去皮/分拣改写；回收积分不再由毛重直接换算
    grams = one(db, 'SELECT COALESCE(SUM(mass_grams),0) AS n FROM waste_drop WHERE household_id=?',
                (household_id,))['n']
    state = 'accepted' if grams else 'pending'
    db.execute('INSERT INTO waste_decision(household_id,accepted_grams,state) VALUES(?,?,?) '
               'ON CONFLICT(household_id) DO UPDATE SET accepted_grams=excluded.accepted_grams,'
               'state=excluded.state', (household_id, grams, state))
    log(db, 'waste', household_id, state,
        'gross deposited material weighed; points require verified actual recycled material', event_id)
    # 此时没有任何实际再生物证据，积分保持 pending，等待 recycling 加工证书
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
