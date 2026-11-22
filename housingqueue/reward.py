from .store import one, many, log


def decide(db, household_id, event_id=None):
    """积分只基于可归属的实际再生物质量（reward_lot），入站毛重不再直接换分。

    迟到的复称或加工证书只修订尚未兑付的积分；已经兑付的部分形成可追偿差额，
    绝不伪造负重量或负积分。
    """
    waste = one(db, 'SELECT * FROM waste_decision WHERE household_id=?', (household_id,))
    gross = waste['accepted_grams'] if waste else 0
    lots = many(db, 'SELECT state, SUM(grams) AS grams, SUM(points) AS points '
                    'FROM reward_lot WHERE household_id=? GROUP BY state', (household_id,))
    lot_by_state = {r['state']: r for r in lots}
    credited = lot_by_state.get('credited', {'grams': 0, 'points': 0})
    frozen = lot_by_state.get('frozen', {'grams': 0, 'points': 0})
    verified_grams = credited['grams']
    current_points = verified_grams // 100
    paid = one(db, 'SELECT COALESCE(SUM(points),0) AS n FROM reward_payout WHERE household_id=?',
               (household_id,))['n']
    recovered = one(db, 'SELECT COALESCE(SUM(points),0) AS n FROM reward_recovery WHERE household_id=?',
                    (household_id,))['n']

    # 已兑付超出当前已核积分：差额登记为可追偿（只记账，不产生负数）。
    # 同一事件对多户分别追偿，靠 (event_id, household_id) 唯一约束防重放重复登记
    if paid > current_points and event_id:
        exists = one(db, 'SELECT 1 FROM reward_recovery WHERE event_id=? AND household_id=?',
                     (event_id, household_id))
        if not exists:
            db.execute('INSERT INTO reward_recovery(event_id,household_id,points,reason) VALUES(?,?,?,?)',
                       (event_id, household_id, paid - current_points,
                        'late re-weigh or certificate revision after payout; gross deposit never rewritten'))

    if not lots and not paid:
        state = 'pending'
    elif paid > current_points:
        state = 'recoverable'
    elif frozen['points'] > 0:
        state = 'frozen'
    elif current_points:
        state = 'credited'
    else:
        state = 'pending'
    basis = ('points from attributable actual recycled material only; '
             f'inbound gross {gross}g kept on waste_decision and never converted directly')
    db.execute('INSERT INTO reward_decision(household_id,points,state,verified_grams,basis) VALUES(?,?,?,?,?) '
               'ON CONFLICT(household_id) DO UPDATE SET points=excluded.points,state=excluded.state,'
               'verified_grams=excluded.verified_grams,basis=excluded.basis',
               (household_id, current_points, state, verified_grams, basis))
    log(db, 'reward', household_id, state,
        f'verified recycled {verified_grams}g -> {current_points} points; paid {paid}, '
        f'frozen {frozen["points"]}, recoverable {max(paid - current_points, 0) - recovered}',
        event_id)
    return state
