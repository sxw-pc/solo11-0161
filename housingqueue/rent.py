from .store import one, log


def decide(db, contract_id, event_id=None):
    due = one(db, 'SELECT COALESCE(SUM(amount_cents),0) AS n FROM rent_invoice WHERE contract_id=?', (contract_id,))['n']
    paid = one(db, 'SELECT COALESCE(SUM(amount_cents),0) AS n FROM rent_receipt WHERE contract_id=?', (contract_id,))['n']
    outstanding = max(0, due - paid)
    state = 'paid' if outstanding == 0 else 'due'
    old = one(db, 'SELECT * FROM rent_decision WHERE contract_id=? ORDER BY id DESC LIMIT 1', (contract_id,))
    if not old or (old['amount_due'], old['amount_paid'], old['outstanding'], old['state']) != (due, paid, outstanding, state):
        db.execute('INSERT INTO rent_decision(contract_id,amount_due,amount_paid,outstanding,state) VALUES(?,?,?,?,?)',
                   (contract_id, due, paid, outstanding, state))
        log(db, 'rent', contract_id, state, 'aggregate invoiced amount minus receipts', event_id)
    return state


def contract(db, e):
    offer = one(db, "SELECT * FROM offer WHERE id=? AND state='signed'", (e['offer_id'],))
    if not offer or int(e['monthly_cents']) <= 0:
        raise ValueError('signed offer and positive monthly rent required')
    db.execute('INSERT INTO rent_contract(id,offer_id,household_id,monthly_cents,starts) VALUES(?,?,?,?,?)',
               (e['id'], offer['id'], offer['household_id'], int(e['monthly_cents']), e['starts']))
    return decide(db, e['id'], e['id'])


def invoice(db, e):
    if not one(db, 'SELECT id FROM rent_contract WHERE id=?', (e['contract_id'],)) or int(e['amount_cents']) < 0:
        raise ValueError('known contract and nonnegative invoice required')
    db.execute('INSERT INTO rent_invoice(id,contract_id,month,amount_cents) VALUES(?,?,?,?)',
               (e['id'], e['contract_id'], e['month'], int(e['amount_cents'])))
    return decide(db, e['contract_id'], e['id'])


def receipt(db, e):
    if not one(db, 'SELECT id FROM rent_contract WHERE id=?', (e['contract_id'],)) or int(e['amount_cents']) <= 0:
        raise ValueError('known contract and positive receipt required')
    db.execute('INSERT INTO rent_receipt(id,contract_id,at,amount_cents) VALUES(?,?,?,?)',
               (e['id'], e['contract_id'], e['at'], int(e['amount_cents'])))
    return decide(db, e['contract_id'], e['id'])
