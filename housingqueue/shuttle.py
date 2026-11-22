from .store import one, log


def vehicle(db, e):
    if int(e['seats']) <= 0 or int(e['wheelchair_places']) < 0:
        raise ValueError('positive seats and nonnegative wheelchair places required')
    db.execute('INSERT INTO shuttle_vehicle(id,seats,wheelchair_places) VALUES(?,?,?)',
               (e['id'], int(e['seats']), int(e['wheelchair_places'])))
    log(db, 'shuttle', e['id'], 'ready', 'vehicle registered', e['id'])
    return 'ready'


def confirmable(db, household_id, at):
    """A household may receive a confirmed seat only after housing delivery.

    Signing an allocation marks the household housed for allocation purposes,
    but the shuttle's confirmable list is keyed on the actual handover time
    recorded by ``allocation.deliver``.
    """
    from .ride_route import delivered_at
    from datetime import datetime
    handed = delivered_at(db, household_id)
    return bool(handed and handed <= datetime.fromisoformat(at))


def bookable_households(db, at):
    from datetime import datetime
    moment = datetime.fromisoformat(at).strftime('%Y-%m-%dT%H:%M:%S')
    return [r['id'] for r in db.execute(
        'SELECT DISTINCT household_id AS id FROM housing_delivery WHERE at<=? ORDER BY household_id',
        (moment,)).fetchall()]


def request(db, e):
    """Legacy single-interval request.

    The decision stays an interval reservation (the baseline behaviour), but
    the reason now distinguishes a signed household whose housing has not yet
    been handed over, which the multi-stop route ledger will not confirm.
    """
    h = one(db, 'SELECT status FROM household WHERE id=?', (e['household_id'],))
    v = one(db, 'SELECT * FROM shuttle_vehicle WHERE id=?', (e['vehicle_id'],))
    if not h or not v or e['from_at'] >= e['to_at'] or int(e['seats']) <= 0:
        raise ValueError('known resident and vehicle with valid travel interval required')
    if h['status'] not in ('housed', 'housed_disputed'):
        state, reason = 'blocked', 'resident not housed'
    elif int(e['seats']) > v['seats'] or int(bool(e.get('wheelchair'))) > v['wheelchair_places']:
        state, reason = 'blocked', 'vehicle capacity incompatible'
    elif one(db, "SELECT 1 FROM shuttle_request r JOIN shuttle_decision d ON d.request_id=r.id WHERE r.vehicle_id=? AND d.state='booked' AND r.from_at<? AND r.to_at>?", (v['id'], e['to_at'], e['from_at'])):
        state, reason = 'conflict', 'vehicle booked for overlapping trip'
    elif not confirmable(db, e['household_id'], e['from_at']):
        state, reason = 'booked', 'interval reserved; housing not yet delivered so route seats stay unconfirmed'
    else:
        state, reason = 'booked', 'resident and vehicle time compatible'
    db.execute('INSERT INTO shuttle_request(id,household_id,vehicle_id,from_at,to_at,seats,wheelchair) VALUES(?,?,?,?,?,?,?)',
               (e['id'], e['household_id'], e['vehicle_id'], e['from_at'], e['to_at'], int(e['seats']), int(bool(e.get('wheelchair')))))
    db.execute('INSERT INTO shuttle_decision(request_id,state,reason) VALUES(?,?,?)', (e['id'], state, reason))
    log(db, 'shuttle', e['id'], state, reason, e['id'])
    return state
