from .store import one, log


def decide(db, household_id, event_id=None):
    waste = one(db, 'SELECT * FROM waste_decision WHERE household_id=?', (household_id,))
    points = waste['accepted_grams'] // 100 if waste else 0
    state = 'credited' if points else 'pending'
    db.execute('INSERT INTO reward_decision(household_id,points,state) VALUES(?,?,?) ON CONFLICT(household_id) DO UPDATE SET points=excluded.points,state=excluded.state',
               (household_id, points, state))
    log(db, 'reward', household_id, state, 'accepted grams converted to points', event_id)
    return state
