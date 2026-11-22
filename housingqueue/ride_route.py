"""Executable multi-stop community shuttle routes.

A route is one physical vehicle trip.  Seats, wheelchair places and the
driver's continuous-driving clock are checked on every travelled segment, and
a household only leaves the vehicle when two witnesses record the alighting.
A signature alone does not make a household bookable: housing must actually be
delivered before the shuttle may promise it a confirmed seat.
"""
from datetime import datetime, timedelta
from itertools import permutations

from .store import one, many, log


class RouteError(ValueError):
    pass


def _p(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise RouteError(f'invalid timestamp {value!r}')


def _s(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%S')


def _place(db, stop_id):
    row = one(db, 'SELECT place_id FROM shuttle_stop WHERE id=?', (stop_id,))
    if not row:
        raise RouteError(f'unknown stop {stop_id}')
    return row['place_id']


# ------------------------------------------------------------------ registry

def place(db, e):
    if not e.get('id'):
        raise RouteError('place id required')
    db.execute('INSERT OR IGNORE INTO shuttle_place(id) VALUES(?)', (e['id'],))
    log(db, 'route', e['id'], 'ready', 'place registered', e['id'])
    return 'ready'


def stop(db, e):
    if not one(db, 'SELECT 1 FROM shuttle_place WHERE id=?', (e['place_id'],)):
        raise RouteError('stop requires a known place')
    db.execute('INSERT INTO shuttle_stop(id,place_id) VALUES(?,?)', (e['id'], e['place_id']))
    log(db, 'route', e['id'], 'ready', 'stop bound to place', e['id'])
    return 'ready'


def segment(db, e):
    minutes = int(e['minutes'])
    if minutes <= 0:
        raise RouteError('segment travel time must be positive')
    if e['from_place'] == e['to_place']:
        raise RouteError('segment endpoints must differ')
    for side in ('from_place', 'to_place'):
        if not one(db, 'SELECT 1 FROM shuttle_place WHERE id=?', (e[side],)):
            raise RouteError('segment requires known places')
    if one(db, 'SELECT 1 FROM shuttle_segment WHERE from_place=? AND to_place=?',
           (e['from_place'], e['to_place'])):
        raise RouteError('segment travel time already registered')
    db.execute('INSERT INTO shuttle_segment(from_place,to_place,minutes) VALUES(?,?,?)',
               (e['from_place'], e['to_place'], minutes))
    log(db, 'route', f"{e['from_place']}->{e['to_place']}", 'ready', f'{minutes} minutes', e['id'])
    return 'ready'


def driver(db, e):
    daily, continuous, rest = (int(e['max_drive_minutes']), int(e['max_continuous_minutes']),
                               int(e['required_rest_minutes']))
    if daily <= 0 or continuous <= 0 or rest < 0 or continuous > daily:
        raise RouteError('driver limits invalid')
    db.execute('INSERT INTO shuttle_driver(id,max_drive_minutes,max_continuous_minutes,required_rest_minutes) VALUES(?,?,?,?)',
               (e['id'], daily, continuous, rest))
    log(db, 'route', e['id'], 'ready', 'driver duty limits registered', e['id'])
    return 'ready'


def shift(db, e):
    if _p(e['starts_at']) >= _p(e['ends_at']):
        raise RouteError('shift must end after it starts')
    if not one(db, 'SELECT 1 FROM shuttle_driver WHERE id=?', (e['driver_id'],)):
        raise RouteError('shift requires a known driver')
    if not one(db, 'SELECT 1 FROM shuttle_vehicle WHERE id=?', (e['vehicle_id'],)):
        raise RouteError('shift requires a known vehicle')
    db.execute('INSERT INTO shuttle_shift(id,driver_id,vehicle_id,starts_at,ends_at) VALUES(?,?,?,?,?)',
               (e['id'], e['driver_id'], e['vehicle_id'], e['starts_at'], e['ends_at']))
    log(db, 'route', e['id'], 'ready', 'driver shift registered', e['id'])
    return 'ready'


def closure(db, e):
    if _p(e['closed_at']) >= _p(e['until']):
        raise RouteError('closure must end after it starts')
    for side in ('from_place', 'to_place'):
        if not one(db, 'SELECT 1 FROM shuttle_place WHERE id=?', (e[side],)):
            raise RouteError('closure requires known places')
    db.execute('INSERT INTO road_closure(id,from_place,to_place,closed_at,until) VALUES(?,?,?,?,?)',
               (e['id'], e['from_place'], e['to_place'], e['closed_at'], e['until']))
    log(db, 'route', e['id'], 'closed', f"road {e['from_place']}->{e['to_place']} unavailable", e['id'])
    return 'closed'


def wheelchair_auth(db, e):
    if not one(db, 'SELECT 1 FROM household WHERE id=?', (e['household_id'],)):
        raise RouteError('wheelchair authorization requires a known household')
    db.execute('INSERT INTO shuttle_wheelchair_auth(id,household_id,granted_at) VALUES(?,?,?)',
               (e['id'], e['household_id'], e['granted_at']))
    log(db, 'route', e['id'], 'granted', 'resident approved wheelchair securement', e['id'])
    return 'granted'


# ------------------------------------------------------------------ context

def delivered_at(db, household_id):
    row = one(db, 'SELECT at FROM housing_delivery WHERE household_id=? ORDER BY at,id LIMIT 1',
              (household_id,))
    return _p(row['at']) if row else None


def _path(db, a, b, depart):
    """Shortest real road path at ``depart``; closed segments are unusable."""
    if a == b:
        return []
    edges = {}
    for row in many(db, 'SELECT * FROM shuttle_segment'):
        edges.setdefault(row['from_place'], []).append((row['to_place'], int(row['minutes'])))
    for row in many(db, 'SELECT * FROM road_closure'):
        closed, until = _p(row['closed_at']), _p(row['until'])
        if closed <= depart < until:
            edges[row['from_place']] = [(to, m) for to, m in edges.get(row['from_place'], [])
                                        if to != row['to_place']]
    best = {a: (0, [])}
    queue = [(0, a, [])]
    while queue:
        queue.sort(key=lambda x: (x[0], x[1]))
        cost, node, trail = queue.pop(0)
        if node == b:
            return trail
        if best.get(node, (None,))[0] != cost:
            continue
        for nxt, minutes in edges.get(node, []):
            new = cost + minutes
            if nxt not in best or new < best[nxt][0]:
                best[nxt] = (new, trail + [(node, nxt, minutes)])
                queue.append((new, nxt, trail + [(node, nxt, minutes)]))
    raise RouteError(f'no open road route {a}->{b} at {_s(depart)}')


def _seats_aboard(db, vehicle_id, at, exclude=None):
    """Households physically on the vehicle at ``at``.

    A seat is released only by a witnessed alighting on that same request;
    the reservation end time is never used as a substitute.
    """
    rows = many(db, """SELECT b.request_id,b.household_id,b.at FROM ride_boarding b
        JOIN ride_booking bk ON bk.request_id=b.request_id
        WHERE bk.vehicle_id=? AND b.kind='board' AND b.at<=?""",
                (vehicle_id, at))
    aboard = []
    for row in rows:
        if exclude and (row['request_id'], row['household_id']) == exclude:
            continue
        left = one(db, """SELECT 1 FROM ride_boarding a WHERE a.request_id=? AND a.household_id=?
                          AND a.kind='alight' AND a.at<=?""", (row['request_id'], row['household_id'], at))
        if not left:
            aboard.append({'request_id': row['request_id'], 'household_id': row['household_id']})
    return aboard


def _seat_check(db, ctx, household_id, at, stop_seq=None):
    """A boarding may not exceed seats physically occupied at that moment.

    The planned leg already accounts for the route's own stops; on top of that
    any resident of another trip who boarded and never had a witnessed
    alighting still holds a seat.
    """
    aboard = _seats_aboard(db, ctx['req']['vehicle_id'], _s(at),
                           exclude=(ctx['req']['id'], household_id))
    seats = len(aboard) + 1
    chairs = sum(1 for x in aboard if _wheelchair_of(db, x['request_id'], x['household_id'])) \
        + _wheelchair(db, ctx, household_id)
    planned = 0
    if stop_seq is not None:
        plan = one(db, 'SELECT * FROM ride_stop_state WHERE request_id=? AND seq=?',
                   (ctx['req']['id'], int(stop_seq)))
        if plan and plan['state'] in ('planned', 'replanned', 'alight_expected'):
            leg = one(db, 'SELECT * FROM ride_leg WHERE booking_id=? AND stop_seq=? ORDER BY seq LIMIT 1',
                      (f"B-{ctx['req']['id']}", int(stop_seq)))
            if leg:
                planned = leg['passengers_in_vehicle']
    seats = max(seats, planned + 1)
    if seats > int(ctx['req']['seats']):
        stuck = aboard[0]['household_id'] if aboard else 'a resident without a witnessed alighting'
        raise RouteError(f'no witnessed alighting: {stuck} still occupies a seat')
    if chairs > int(ctx['req']['wheelchair_places']):
        raise RouteError('wheelchair places are still occupied')


def _wheelchair_of(db, request_id, household_id):
    row = one(db, 'SELECT MAX(wheelchair) AS w FROM ride_request_stop WHERE request_id=? AND household_id=?',
              (request_id, household_id))
    return int(row['w'] or 0) if row else 0


def _recount_legs(db, request_id):
    """Recompute planned leg occupancy from witnessed boarding/alighting evidence.

    Travelled legs keep their observed numbers; future legs are re-evaluated so
    a resident with no witnessed alighting keeps occupying a seat.
    """
    ctx = _context(db, request_id)
    booking = _booking(db, request_id)
    if not booking:
        return
    for leg in many(db, "SELECT * FROM ride_leg WHERE booking_id=? AND status='planned' ORDER BY seq",
                    (booking['id'],)):
        aboard = _seats_aboard(db, ctx['req']['vehicle_id'], leg['depart_at'])
        seats = len(aboard)
        chairs = sum(1 for x in aboard if _wheelchair_of(db, x['request_id'], x['household_id']))
        db.execute('UPDATE ride_leg SET passengers_in_vehicle=?,wheelchair_in_vehicle=? WHERE id=?',
                   (seats, chairs, leg['id']))


def _aboard_now(db, request_id, at):
    """Households already aboard this request at ``at`` (pickup done, no alight)."""
    rows = many(db, """SELECT household_id FROM ride_boarding WHERE request_id=? AND kind='board' AND at<=?""",
                (request_id, _s(at)))
    aboard = []
    for row in rows:
        if not one(db, "SELECT 1 FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='alight' AND at<=?",
                   (request_id, row['household_id'], _s(at))):
            aboard.append(row['household_id'])
    return aboard


def _context(db, request_id):
    req = one(db, 'SELECT * FROM ride_request WHERE id=?', (request_id,))
    if not req:
        raise RouteError('unknown ride request')
    shift = one(db, 'SELECT * FROM shuttle_shift WHERE id=?', (req['shift_id'],))
    driver = one(db, 'SELECT * FROM shuttle_driver WHERE id=?', (req['driver_id'],))
    vehicle = one(db, 'SELECT * FROM shuttle_vehicle WHERE id=?', (req['vehicle_id'],))
    if not shift or not driver or not vehicle:
        raise RouteError('ride request references an unknown shift, driver or vehicle')
    stops = [dict(row) for row in many(db, 'SELECT * FROM ride_request_stop WHERE request_id=? ORDER BY seq',
                                       (request_id,))]
    if not stops:
        raise RouteError('ride request has no stops')
    for row in stops:
        row['place_id'] = _place(db, row['stop_id'])
    households = sorted({s['household_id'] for s in stops if s['kind'] in ('pickup', 'dropoff')})
    for h in households:
        if not one(db, 'SELECT 1 FROM household WHERE id=?', (h,)):
            raise RouteError(f'ride household {h} is not registered')
        picks = [s for s in stops if s['household_id'] == h and s['kind'] == 'pickup']
        drops = [s for s in stops if s['household_id'] == h and s['kind'] == 'dropoff']
        if bool(picks) != bool(drops):
            raise RouteError(f'{h} needs both a pickup and a dropoff')
    return {'req': req, 'shift': shift, 'driver': driver, 'vehicle': vehicle, 'stops': stops,
            'households': households, 'origin': _place(db, req['origin_stop']),
            'start': _p(req['starts_at']), 'aboard': [],
            'wheelchair': {h: max(int(s['wheelchair']) for s in stops if s['household_id'] == h)
                           for h in households},
            'lingering': _lingering(db, req['vehicle_id'], request_id)}


def _lingering(db, vehicle_id, request_id):
    """Residents still occupying a seat because no alighting was witnessed."""
    rows = many(db, """SELECT DISTINCT b.request_id,b.household_id FROM ride_boarding b
        JOIN ride_booking bk ON bk.request_id=b.request_id
        WHERE bk.vehicle_id=? AND b.kind='board'
        AND NOT EXISTS (SELECT 1 FROM ride_boarding a WHERE a.request_id=b.request_id
                        AND a.household_id=b.household_id AND a.kind='alight')""",
                (vehicle_id,))
    return sorted({r['household_id'] for r in rows if r['request_id'] != request_id})


def _pickup_before_dropoff(order, aboard=()):
    """A dropoff is legal if the pickup precedes it or the rider is already aboard."""
    picked = set(aboard)
    for s in order:
        if s['kind'] == 'pickup':
            picked.add(s['household_id'])
        elif s['kind'] == 'dropoff' and s['household_id'] not in picked:
            return False
    return True


def _route_orders(stops, aboard=()):
    points = [s for s in stops if s['kind'] in ('pickup', 'dropoff')]
    for combo in permutations(points):
        if _pickup_before_dropoff(combo, aboard):
            yield list(combo)


def _riders(ctx, order, upto):
    """Which households are physically aboard after serving ``order[:upto]``."""
    aboard, off = list(ctx.get('aboard') or []), set()
    for s in order[:upto]:
        if s['kind'] == 'pickup':
            aboard.append(s['household_id'])
        else:
            off.add(s['household_id'])
    return [h for h in ctx['households'] if h in aboard and h not in off]


def _housed_before(db, ctx, household_id, moment):
    """Housing must be delivered before a confirmed seat is promised."""
    at = delivered_at(db, household_id)
    return bool(at and at <= moment) and at <= moment


def _simulate(db, ctx, order, start=None):
    """Walk the stops over real road segments, checking every physical limit."""
    moment = start or ctx['start']
    origin = ctx['origin']
    legs, plan = [], []
    drive_total = 0
    continuous = 0
    for index, stopped in enumerate(order):
        place = stopped['place_id']
        current = legs[-1]['to_place'] if legs else origin
        path = _path(db, current, place, moment)
        total = sum(m for _, _, m in path)
        max_continuous = int(ctx['driver']['max_continuous_minutes'])
        rest = 0
        if total and continuous and continuous + total > max_continuous:
            rest = int(ctx['driver']['required_rest_minutes'])
            continuous = 0
            moment += timedelta(minutes=rest)
            path = _path(db, current, place, moment)
        in_vehicle = _riders(ctx, order, index)
        loading = _riders(ctx, order, index + 1)
        # At plan time the order frees a seat at a dropoff; but a seat is only
        # genuinely free once the alighting is witnessed.  A rider who boarded
        # and whose dropoff has no witnessed alighting by this moment keeps the
        # seat on top of the planned load.
        freed_on_paper = {s['household_id'] for s in order[:index] if s['kind'] == 'dropoff'}
        unwitnessed = []
        for h in freed_on_paper:
            if h in loading:
                continue
            if one(db, "SELECT 1 FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='alight' AND at<=?",
                   (ctx['req']['id'], h, _s(moment))):
                continue
            if one(db, "SELECT 1 FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='board'",
                   (ctx['req']['id'], h)):
                unwitnessed.append(h)
        lingering = [h for h in ctx['lingering'] if h not in loading]
        capacity = len(loading) + len(lingering) + len(unwitnessed)
        if capacity > int(ctx['req']['seats']):
            stuck = (lingering + unwitnessed)
            raise RouteError(f"route exceeds vehicle seats at {stopped['stop_id']}"
                             + (f"; {stuck[0]} never witnessed alighting" if stuck else ''))
        chairs = sum(1 for h in in_vehicle if _wheelchair(db, ctx, h))
        if chairs > int(ctx['req']['wheelchair_places']):
            raise RouteError('route exceeds vehicle wheelchair places')
        cursor = moment
        if not path:
            legs.append({'stop_seq': stopped['seq'], 'from_place': current, 'to_place': place,
                         'depart_at': _s(cursor), 'arrive_at': _s(cursor), 'drive_minutes': 0,
                         'break_minutes': rest, 'passengers_in_vehicle': len(in_vehicle) + len(lingering),
                         'wheelchair_in_vehicle': chairs})
        for hop, (a, b, minutes) in enumerate(path):
            legs.append({'stop_seq': stopped['seq'], 'from_place': a, 'to_place': b,
                         'depart_at': _s(cursor), 'arrive_at': _s(cursor + timedelta(minutes=minutes)),
                         'drive_minutes': minutes,
                         'break_minutes': rest if hop == 0 else 0,
                         'passengers_in_vehicle': len(in_vehicle) + len(lingering),
                         'wheelchair_in_vehicle': chairs})
            cursor += timedelta(minutes=minutes)
            drive_total += minutes
            continuous += minutes
        if cursor > _p(ctx['shift']['ends_at']):
            raise RouteError('planned route runs past the driver shift')
        if drive_total > int(ctx['driver']['max_drive_minutes']):
            raise RouteError('driver shift driving limit exceeded')
        plan.append({'stop': stopped, 'arrive': cursor, 'leg': legs[-1]})
        moment = cursor
    return legs, plan


def _wheelchair(db, ctx, household_id):
    if ctx.get('wheelchair') is not None:
        return ctx['wheelchair'].get(household_id, 0)
    row = one(db, 'SELECT MAX(wheelchair) AS w FROM ride_request_stop WHERE request_id=? AND household_id=?',
              (ctx['req']['id'], household_id))
    return int(row['w'] or 0)


# ------------------------------------------------------------------ booking


def _window_ok(db, ctx, household_id, moment):
    """Arrival must fall inside the hours the household actually authorized.

    An original window sets the earliest acceptable arrival and the original
    deadline; a written rebook (hospital authorization or a resident's own
    extension) extends that deadline.  Arriving early remains acceptable.
    """
    rows = [dict(r) for r in many(
        db, 'SELECT * FROM ride_authorization WHERE request_id=? AND household_id=? ORDER BY recorded_at,id',
        (ctx['req']['id'], household_id))]
    if not rows:
        raise RouteError(f'no written authorization for {household_id}')
    earliest = min(_p(r['window_start']) for r in rows)
    latest = max(_p(r['window_end']) for r in rows)
    return earliest <= moment <= latest, rows


def _bookable_at(db, ctx, household_id, moment):
    """A confirmed seat needs delivered housing no later than the ride."""
    arrival = delivered_at(db, household_id)
    if arrival is None:
        raise RouteError(f'{household_id} has no delivered housing')
    if arrival > moment:
        raise RouteError(f'{household_id} housing delivered after this ride')
    if not one(db, 'SELECT 1 FROM shuttle_wheelchair_auth WHERE household_id=? AND granted_at<=?',
               (household_id, _s(ctx['start']))) and _wheelchair(db, ctx, household_id):
        raise RouteError(f'{household_id} has not approved wheelchair securement')
    return True


def _best_order(db, ctx, stops, start=None):
    """Pick a feasible physical order, preferring both windows then least lateness."""
    best = None
    failures = []
    for order in _route_orders(stops):
        try:
            legs, plan = _simulate(db, ctx, order, start=start)
        except RouteError as exc:
            failures.append(str(exc))
            continue
        late = 0
        outside = []
        for item in plan:
            s = item['stop']
            try:
                _bookable_at(db, ctx, s['household_id'], item['arrive'])
            except RouteError:
                outside.append(f"{s['household_id']} not bookable")
                continue
            ok, _ = _window_ok(db, ctx, s['household_id'], item['arrive'])
            if not ok:
                outside.append(f"{s['stop_id']} outside authorized window")
            if s['appointment_at']:
                late += max(0, int((item['arrive'] - _p(s['appointment_at'])).total_seconds() // 60))
        key = (bool(outside), late, [item['stop']['stop_id'] for item in plan])
        if best is None or key < best[0]:
            best = (key, order, legs, plan, outside)
    if best is None:
        raise RouteError(failures[0] if failures else 'no feasible stop order')
    return best[1], best[2], best[3], best[4]


def request(db, e):
    """Record a ride request.  A not-yet-housed household may be consulted only."""
    if not one(db, 'SELECT 1 FROM shuttle_shift WHERE id=?', (e['shift_id'],)):
        raise RouteError('ride request requires a known shift')
    row = one(db, 'SELECT * FROM shuttle_shift WHERE id=?', (e['shift_id'],))
    if row['vehicle_id'] != e['vehicle_id'] or row['driver_id'] != e['driver_id']:
        raise RouteError('vehicle and driver must match the registered shift')
    if not one(db, 'SELECT 1 FROM shuttle_vehicle WHERE id=?', (e['vehicle_id'],)):
        raise RouteError('ride request requires a registered vehicle')
    if int(e['seats']) <= 0 or int(e['wheelchair_places']) < 0:
        raise RouteError('positive seats and nonnegative wheelchair places required')
    if not one(db, 'SELECT 1 FROM shuttle_stop WHERE id=?', (e['origin_stop'],)):
        raise RouteError('ride request requires a known origin stop')
    if _p(e['starts_at']) < _p(row['starts_at']) or _p(e['starts_at']) > _p(row['ends_at']):
        raise RouteError('ride request starts outside the driver shift')
    db.execute('INSERT INTO ride_request(id,vehicle_id,shift_id,driver_id,origin_stop,starts_at,seats,wheelchair_places,context_only,state,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (e['id'], e['vehicle_id'], e['shift_id'], e['driver_id'], e['origin_stop'], e['starts_at'],
                int(e['seats']), int(e['wheelchair_places']), int(bool(e.get('context_only'))), 'recorded',
                e.get('reason', '')))
    log(db, 'route', e['id'], 'recorded', 'ride request recorded', e['id'])
    return 'recorded'


def request_stop(db, e):
    if not one(db, 'SELECT 1 FROM ride_request WHERE id=?', (e['request_id'],)):
        raise RouteError('ride stop requires a known request')
    stop_kind = e.get('stop_kind', e.get('kind'))
    if stop_kind not in ('pickup', 'dropoff'):
        raise RouteError('invalid ride stop kind')
    if one(db, 'SELECT 1 FROM ride_request_stop WHERE request_id=? AND seq=?',
           (e['request_id'], int(e['seq']))):
        raise RouteError('duplicate stop sequence')
    if not one(db, 'SELECT 1 FROM household WHERE id=?', (e['household_id'],)):
        raise RouteError('passenger stop requires a known household')
    if _p(e['earliest']) > _p(e['latest']):
        raise RouteError('authorized window is inverted')
    db.execute('INSERT INTO ride_request_stop(request_id,seq,household_id,stop_id,place_id,kind,earliest,latest,wheelchair,appointment_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['request_id'], int(e['seq']), e['household_id'], e['stop_id'], _place(db, e['stop_id']),
                stop_kind, e['earliest'], e['latest'], int(bool(e.get('wheelchair'))), e.get('appointment_at')))
    log(db, 'route', f"{e['request_id']}/{e['seq']}", 'recorded', f"{stop_kind} {e['stop_id']}", e['id'])
    return 'recorded'


def authorize(db, e):
    if not one(db, 'SELECT 1 FROM ride_request WHERE id=?', (e['request_id'],)):
        raise RouteError('authorization requires a known ride request')
    auth_kind = e.get('auth_kind', e.get('window_kind'))
    if auth_kind not in ('original', 'rebook'):
        raise RouteError('authorization kind must be original or rebook')
    if not e.get('recorded_by'):
        raise RouteError('authorization requires the recording agent')
    if _p(e['window_start']) > _p(e['window_end']):
        raise RouteError('authorization window is inverted')
    db.execute('INSERT INTO ride_authorization(id,request_id,household_id,window_start,window_end,kind,recorded_at,recorded_by) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'], e['request_id'], e['household_id'], e['window_start'], e['window_end'], auth_kind,
                e.get('recorded_at') or e['window_start'], e['recorded_by']))
    log(db, 'route', e['id'], 'authorized', f"{auth_kind} window for {e['household_id']}", e['id'])
    return 'authorized'


def hospital_rebook(db, e):
    pick = one(db, "SELECT * FROM ride_request_stop WHERE request_id=? AND household_id=? AND kind='pickup' ORDER BY seq LIMIT 1",
               (e['request_id'], e['household_id']))
    if not pick:
        raise RouteError('hospital rebook requires the household on this ride')
    if not e.get('certificate'):
        raise RouteError('hospital rebook requires a certificate reference')
    if _p(e['new_appointment_at']) <= _p(pick['earliest']):
        raise RouteError('rebooked appointment must be later than the original')
    db.execute('INSERT INTO hospital_rebook(id,household_id,request_id,at,new_appointment_at,certificate) VALUES(?,?,?,?,?,?)',
               (e['id'], e['household_id'], e['request_id'], e['at'], e['new_appointment_at'],
                e['certificate']))
    db.execute('INSERT INTO ride_authorization(id,request_id,household_id,window_start,window_end,kind,recorded_at,recorded_by) VALUES(?,?,?,?,?,?,?,?)',
               (f"{e['id']}-AUTH", e['request_id'], e['household_id'], e['new_appointment_at'],
                e['new_appointment_at'], 'rebook', e['at'], 'hospital'))
    log(db, 'route', e['id'], 'rebooked', 'hospital authorized a later appointment', e['id'])
    return 'rebooked'


def consult(db, e):
    """Line advice only: refuses to turn a consultation into a booking."""
    if not one(db, 'SELECT 1 FROM ride_request WHERE id=?', (e['request_id'],)):
        raise RouteError('consultation requires a known ride request')
    if not one(db, 'SELECT 1 FROM household WHERE id=?', (e['household_id'],)):
        raise RouteError('consultation requires a known household')
    when = _p(e['at'])
    housed = delivered_at(db, e['household_id'])
    answer = 'bookable' if housed and housed <= when else 'consultation_only'
    db.execute('INSERT INTO ride_consultation(id,request_id,household_id,at,answer) VALUES(?,?,?,?,?)',
               (e['id'], e['request_id'], e['household_id'], e['at'], answer))
    log(db, 'route', e['id'], 'consulted', 'no confirmed seat promised before housing delivery', e['id'])
    return 'advised'


def delivery(db, e):
    """Fixture path for handover when allocation events live in another file.

    Mirrors ``allocation.deliver``: refuses unsigned offers, unknown receivers
    and duplicate handovers.  Normal use is the allocation ``deliver`` event.
    """
    from .allocation import deliver
    return deliver(db, e)


# ------------------------------------------------------------------ execution

def _booking(db, request_id):
    return one(db, 'SELECT * FROM ride_booking WHERE request_id=? ORDER BY rowid DESC LIMIT 1', (request_id,))


def _legs(db, booking_id):
    return [dict(r) for r in many(db, 'SELECT * FROM ride_leg WHERE booking_id=? ORDER BY seq', (booking_id,))]


def execute(db, e):
    """Commit a physical route.  Only delivered households receive a seat."""
    request_id, at = e['request_id'], _p(e['at'])
    if one(db, 'SELECT 1 FROM ride_booking WHERE request_id=?', (request_id,)):
        raise RouteError('ride already has a committed route')
    ctx = _context(db, request_id)
    if ctx['req']['context_only']:
        raise RouteError('a consultation request cannot be confirmed')
    served = []
    for h in ctx['households']:
        try:
            _bookable_at(db, ctx, h, ctx['start'])
            served.append(h)
        except RouteError as exc:
            for s in ctx['stops']:
                if s['household_id'] == h:
                    db.execute('INSERT OR REPLACE INTO ride_stop_state(request_id,seq,state,at,reason) VALUES(?,?,?,?,?)',
                               (request_id, s['seq'], 'unserved', _s(at), str(exc)))
            _household_report(db, request_id, h)
    if not served:
        raise RouteError('no household is bookable: housing not delivered')
    stops = [s for s in ctx['stops'] if s['household_id'] in served]
    start = ctx['start']
    origin = ctx['origin']
    if e.get('from_stop_seq') is not None:
        cutoff = int(e['from_stop_seq'])
        stops = [s for s in stops if s['seq'] > cutoff]
        if not stops:
            raise RouteError('no stops remain after the closure')
        start = _p(e['at'])
        origin = _place(db, one(db, 'SELECT stop_id FROM ride_request_stop WHERE request_id=? AND seq=?',
                                (request_id, cutoff))['stop_id'])
        ctx = {**ctx, 'start': start, 'origin': origin}
    found = _best_order(db, ctx, stops, start=start)
    order, legs, plan = found[0], found[1], found[2]
    span_start, span_end = legs[0]['depart_at'], legs[-1]['arrive_at']
    clash = one(db, """SELECT * FROM ride_booking WHERE vehicle_id=? AND request_id<>?
        AND state IN ('planned','replanned','infeasible') AND depart_at<? AND arrive_at>?""",
                (ctx['req']['vehicle_id'], request_id, span_end, span_start))
    if clash:
        raise RouteError('vehicle already promised to another trip; one vehicle cannot be at two stops')
    booking_id = f'B-{request_id}'
    db.execute('INSERT INTO ride_booking(id,request_id,vehicle_id,shift_id,driver_id,origin_place,depart_at,arrive_at,state,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (booking_id, request_id, ctx['req']['vehicle_id'], ctx['req']['shift_id'], ctx['req']['driver_id'],
                origin, _s(start), legs[-1]['arrive_at'], 'planned',
                'route verified against residents, capacity, securement and driver clock', _s(at)))
    for leg in legs:
        db.execute('INSERT INTO ride_leg(booking_id,seq,stop_seq,from_place,to_place,depart_at,arrive_at,drive_minutes,break_minutes,passengers_in_vehicle,wheelchair_in_vehicle,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (booking_id, leg['stop_seq'], leg['stop_seq'], leg['from_place'], leg['to_place'],
                    leg['depart_at'], leg['arrive_at'], leg['drive_minutes'], leg['break_minutes'],
                    leg['passengers_in_vehicle'], leg['wheelchair_in_vehicle'], 'planned',
                    'seats, wheelchair places and driver clock verified'))
    for row in ctx['stops']:
        if row['household_id'] not in served or row not in stops:
            if row['household_id'] in served:
                db.execute("INSERT OR REPLACE INTO ride_stop_state(request_id,seq,state,at,reason) VALUES(?,?,?,?,?)",
                           (request_id, row['seq'], 'retained', _s(at), 'already travelled before replan'))
            continue
        db.execute("INSERT OR REPLACE INTO ride_stop_state(request_id,seq,state,at,reason) VALUES(?,?,?,?,?)",
                   (request_id, row['seq'], 'planned', None, 'planned in committed route'))
    _refresh_reports(db, request_id, ctx)
    log(db, 'route', request_id, 'planned', 'executable multi-stop route committed', request_id)
    return 'planned'


def secure(db, e):
    """Wheelchair tiedown or release; two distinct witnesses are mandatory."""
    move = e.get('move')
    if move not in ('secure', 'release'):
        raise RouteError('securement move must be secure or release')
    if not e.get('witness_a') or not e.get('witness_b') or e['witness_a'] == e['witness_b']:
        raise RouteError('two distinct witnesses are required')
    ctx = _context(db, e['request_id'])
    if not _wheelchair(db, ctx, e['household_id']):
        raise RouteError('household did not request a wheelchair place')
    if not _housed_before(db, ctx, e['household_id'], _p(e['at'])):
        raise RouteError('wheelchair household has no delivered housing')
    if move == 'secure' and one(db, "SELECT 1 FROM ride_secure WHERE request_id=? AND household_id=? AND kind='secure'",
                                (e['request_id'], e['household_id'])):
        raise RouteError('wheelchair already secured')
    if move == 'release' and not one(db, "SELECT 1 FROM ride_secure WHERE request_id=? AND household_id=? AND kind='secure'",
                                     (e['request_id'], e['household_id'])):
        raise RouteError('wheelchair was never secured')
    db.execute('INSERT INTO ride_secure(id,request_id,household_id,kind,at,witness_a,witness_b) VALUES(?,?,?,?,?,?,?)',
               (e['id'], e['request_id'], e['household_id'], move, e['at'], e['witness_a'],
                e['witness_b']))
    log(db, 'route', e['id'], move, 'wheelchair tiedown witnessed', e['id'])
    return move


def board(db, e):
    """Witnessed boarding or alighting; an alighting is the only seat release."""
    move = e.get('move')
    if move not in ('board', 'alight'):
        raise RouteError('boarding move must be board or alight')
    if not e.get('witness_a') or not e.get('witness_b') or e['witness_a'] == e['witness_b']:
        raise RouteError('two distinct witnesses are required')
    ctx = _context(db, e['request_id'])
    stop = one(db, 'SELECT * FROM ride_request_stop WHERE request_id=? AND seq=?',
               (e['request_id'], int(e['stop_seq'])))
    if not stop:
        raise RouteError('boarding requires a planned stop of this request')
    if move == 'board' and stop['kind'] != 'pickup':
        raise RouteError('boarding only at a pickup stop')
    if move == 'alight' and stop['kind'] != 'dropoff':
        raise RouteError('alighting only at a dropoff stop')
    booking = _booking(db, e['request_id'])
    if not booking:
        raise RouteError('boarding requires a committed route')
    at = _p(e['at'])
    leg = one(db, 'SELECT * FROM ride_leg WHERE booking_id=? AND stop_seq=?', (booking['id'], int(e['stop_seq'])))
    if not leg:
        raise RouteError('stop is not in the committed route')
    if move == 'board' and at < _p(leg['arrive_at']):
        raise RouteError('boarding cannot precede the vehicle reaching the stop')
    if move == 'alight' and at < _p(leg['arrive_at']):
        raise RouteError('alighting cannot precede the vehicle reaching the stop')
    if e['household_id'] != stop['household_id']:
        raise RouteError('witnessed household does not match the stop')
    if move == 'board':
        if one(db, "SELECT 1 FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='board'",
               (e['request_id'], e['household_id'])):
            raise RouteError('household already boarded')
        if _wheelchair(db, ctx, e['household_id']):
            secured = one(db, "SELECT 1 FROM ride_secure WHERE request_id=? AND household_id=? AND kind='secure'",
                          (e['request_id'], e['household_id']))
            if not secured:
                raise RouteError('wheelchair place requires witnessed securement before boarding')
        _seat_check(db, ctx, e['household_id'], at, stop_seq=int(e['stop_seq']))
    else:
        if not one(db, "SELECT 1 FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='board'",
                   (e['request_id'], e['household_id'])):
            raise RouteError('cannot alight before boarding')
        if one(db, "SELECT 1 FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='alight'",
               (e['request_id'], e['household_id'])):
            raise RouteError('household already alighted')
    db.execute('INSERT INTO ride_boarding(id,request_id,household_id,stop_seq,kind,at,witness_a,witness_b) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'], e['request_id'], e['household_id'], int(e['stop_seq']), move, e['at'],
                e['witness_a'], e['witness_b']))
    db.execute("INSERT OR REPLACE INTO ride_stop_state(request_id,seq,state,at,reason) VALUES(?,?,?,?,?)",
               (e['request_id'], int(e['stop_seq']), 'boarded' if move == 'board' else 'alighted',
                e['at'], 'two-witness event recorded'))
    if move == 'alight':
        db.execute("UPDATE ride_leg SET status='travelled',run_at=? WHERE booking_id=? AND seq<=?",
                   (e['at'], booking['id'], int(e['stop_seq'])))
    _recount_legs(db, e['request_id'])
    _refresh_reports(db, e['request_id'], ctx)
    log(db, 'route', e['id'], move, f"witnessed by {e['witness_a']},{e['witness_b']}", e['id'])
    return 'recorded'


def observe_closure(db, e):
    """Mid-trip closure: travelled work is frozen and only the tail is replanned."""
    request_id = e['request_id']
    ctx = _context(db, request_id)
    row = one(db, 'SELECT * FROM road_closure WHERE id=?', (e['closure_id'],))
    if not row:
        raise RouteError('unknown road closure')
    booking = _booking(db, request_id)
    if not booking:
        raise RouteError('ride has no committed route')
    legs = _legs(db, booking['id'])
    if not legs:
        raise RouteError('ride has no legs')
    hit_index = None
    for index, leg in enumerate(legs):
        if leg['status'] == 'travelled':
            continue
        if (leg['from_place'], leg['to_place']) == (row['from_place'], row['to_place']):
            hit_index = index
            break
    if hit_index is None:
        db.execute("UPDATE ride_booking SET reason=? WHERE id=?",
                   ('closure does not touch an untravelled leg', booking['id']))
        log(db, 'route', request_id, 'unaffected', 'closure not on remaining route', e['closure_id'])
        return 'unaffected'
    if hit_index == 0:
        raise RouteError('the vehicle has not travelled any segment yet; replan the whole trip')
    at = _p(e['at'])
    if at < _p(legs[hit_index]['depart_at']):
        raise RouteError('closure recorded before the vehicle would have departed')
    for index, leg in enumerate(legs[:hit_index]):
        if leg['status'] != 'travelled':
            boarded = one(db, "SELECT at FROM ride_boarding WHERE request_id=? AND stop_seq=? AND kind='alight'",
                          (request_id, leg['stop_seq']))
            if boarded and _p(boarded['at']) > at:
                raise RouteError('cannot freeze a leg whose alighting postdates the closure')
            db.execute("UPDATE ride_leg SET status='travelled',run_at=? WHERE booking_id=? AND seq=?",
                       (boarded['at'] if boarded else _s(min(at, _p(leg['arrive_at']))), booking['id'], leg['seq']))
    travelled = legs[:hit_index]
    cursor = max(_p(x['arrive_at']) for x in travelled)
    if at > cursor:
        cursor = at
    origin = travelled[-1]['to_place']
    done = {x['stop_seq'] for x in travelled}
    left = [s for s in ctx['stops'] if s['seq'] not in done]
    if not left:
        db.execute("UPDATE ride_booking SET state='completed',reason=? WHERE id=?",
                   ('closure after the final dropoff', booking['id']))
        _refresh_reports(db, request_id, ctx)
        log(db, 'route', request_id, 'completed', 'closure after final stop', e['closure_id'])
        return 'completed'
    sub = {**ctx, 'stops': left, 'start': cursor, 'origin': origin,
           'aboard': _aboard_now(db, request_id, cursor)}
    best, failures = None, []
    for order in _route_orders(left, sub['aboard']):
        try:
            tail_legs, plan = _simulate(db, sub, order, start=cursor)
        except RouteError as exc:
            failures.append(str(exc))
            continue
        bad = []
        for item in plan:
            ok, _ = _window_ok(db, ctx, item['stop']['household_id'], item['arrive'])
            if not ok:
                bad.append(item['stop']['stop_id'])
        if bad:
            failures.append(f'{bad[0]} outside authorized window')
            continue
        key = (sum(max(0, int((item['arrive'] - _p(item['stop']['appointment_at'])).total_seconds() // 60))
                   for item in plan if item['stop']['appointment_at']),
               [x['to_place'] for x in tail_legs])
        if best is None or key < best[0]:
            best = (key, order, tail_legs, plan)
    db.execute('DELETE FROM ride_leg WHERE booking_id=? AND status=?', (booking['id'], 'planned'))
    if best is None:
        for s in left:
            db.execute("INSERT OR REPLACE INTO ride_stop_state(request_id,seq,state,at,reason) VALUES(?,?,?,?,?)",
                       (request_id, s['seq'], 'unreachable', _s(cursor),
                        failures[0] if failures else 'no feasible remaining route'))
        db.execute("UPDATE ride_booking SET state='infeasible',reason=? WHERE id=?",
                   ('closure leaves no executable tail', booking['id']))
        _refresh_reports(db, request_id, ctx)
        log(db, 'route', request_id, 'infeasible', 'no executable remaining route', e['closure_id'])
        return 'infeasible'
    _, order, tail_legs, plan = best
    for leg in tail_legs:
        db.execute('INSERT INTO ride_leg(booking_id,seq,stop_seq,from_place,to_place,depart_at,arrive_at,drive_minutes,break_minutes,passengers_in_vehicle,wheelchair_in_vehicle,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (booking['id'], leg['stop_seq'], leg['stop_seq'], leg['from_place'], leg['to_place'],
                    leg['depart_at'], leg['arrive_at'], leg['drive_minutes'], leg['break_minutes'],
                    leg['passengers_in_vehicle'], leg['wheelchair_in_vehicle'], 'planned',
                    'replanned after road closure'))
    for s in left:
        db.execute("INSERT OR REPLACE INTO ride_stop_state(request_id,seq,state,at,reason) VALUES(?,?,?,?,?)",
                   (request_id, s['seq'], 'replanned', _s(cursor), 'closure replanned remaining stops'))
    db.execute("UPDATE ride_booking SET state='replanned',reason=?,arrive_at=? WHERE id=?",
               ('travelled stops retained, tail replanned', tail_legs[-1]['arrive_at'], booking['id']))
    _refresh_reports(db, request_id, ctx)
    log(db, 'route', request_id, 'replanned', 'travelled stops preserved', e['closure_id'])
    return 'replanned'


# ------------------------------------------------------------------ reports

def _refresh_reports(db, request_id, ctx):
    for h in ctx['households']:
        _household_report(db, request_id, h)


def _household_report(db, request_id, household_id):
    """Per-resident evidence: original window, authorized window and real times."""
    stops = [dict(r) for r in many(db, 'SELECT * FROM ride_request_stop WHERE request_id=? AND household_id=? ORDER BY seq',
                                   (request_id, household_id))]
    pick = next((s for s in stops if s['kind'] == 'pickup'), None)
    drop = next((s for s in reversed(stops) if s['kind'] == 'dropoff'), None)
    if not pick and not drop:
        return
    auths = [dict(r) for r in many(db, 'SELECT * FROM ride_authorization WHERE request_id=? AND household_id=? ORDER BY recorded_at,id',
                                   (request_id, household_id))]
    original = next((a for a in auths if a['kind'] == 'original'), None)
    rebook = next((a for a in reversed(auths) if a['kind'] == 'rebook'), None)
    boarded = one(db, "SELECT at FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='board' ORDER BY at,id LIMIT 1",
                  (request_id, household_id))
    alighted = one(db, "SELECT at FROM ride_boarding WHERE request_id=? AND household_id=? AND kind='alight' ORDER BY at DESC,id DESC LIMIT 1",
                   (request_id, household_id))
    boarding_stop = one(db, "SELECT state,reason FROM ride_stop_state WHERE request_id=? AND seq=?",
                        (request_id, pick['seq'])) if pick else None
    appointment = pick['appointment_at'] if pick else None
    boarded_at = boarded['at'] if boarded else None
    alighted_at = alighted['at'] if alighted else None
    if boarding_stop is None and drop:
        verdict = 'unserved'
        reason = 'housing not delivered; no confirmed seat could be offered'
        caught = None
    elif boarding_stop and boarding_stop['state'] == 'unserved':
        verdict, reason, caught = 'unserved', boarding_stop['reason'], None
    elif boarding_stop and boarding_stop['state'] == 'unreachable':
        verdict, reason, caught = 'unreachable', boarding_stop['reason'], None
    elif not drop or not alighted_at:
        verdict = 'partial'
        reason = 'arrival unproven: no witnessed alighting' if drop else 'no delivery stop planned'
        caught = None
    elif appointment is None:
        verdict, reason, caught = 'fulfilled', 'delivered; no original appointment to miss', None
    elif _p(alighted_at) <= _p(appointment):
        verdict, reason, caught = 'fulfilled', 'delivered before the original appointment', 1
    else:
        verdict, reason, caught = 'late', 'delivered after the original appointment', 0
    # "本人授权的改约窗": the latest written extension the resident/hospital
    # gave, or the original window when nothing was changed.
    effective = rebook or original
    db.execute('INSERT OR REPLACE INTO ride_household_report(request_id,household_id,original_window_start,original_window_end,authorized_window_start,authorized_window_end,boarded_at,alighted_at,appointment_at,caught_original,verdict,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (request_id, household_id,
                original['window_start'] if original else None,
                original['window_end'] if original else None,
                effective['window_start'] if effective else None,
                effective['window_end'] if effective else None,
                boarded_at, alighted_at, appointment, caught, verdict, reason))


def route_report(db, request_id):
    """One trip's physical legs, stop states and per-household evidence."""
    booking = _booking(db, request_id)
    return {'request_id': request_id,
            'request': dict(one(db, 'SELECT * FROM ride_request WHERE id=?', (request_id,)) or {}),
            'booking': dict(booking) if booking else None,
            'legs': [dict(r) for r in many(
                db, 'SELECT * FROM ride_leg WHERE booking_id=? ORDER BY seq',
                (booking['id'],))] if booking else [],
            'stops': [dict(r) for r in many(db, 'SELECT * FROM ride_stop_state WHERE request_id=? ORDER BY seq',
                                            (request_id,))],
            'boardings': [dict(r) for r in many(db, 'SELECT * FROM ride_boarding WHERE request_id=? ORDER BY at,id',
                                                (request_id,))],
            'securement': [dict(r) for r in many(db, 'SELECT * FROM ride_secure WHERE request_id=? ORDER BY at,id',
                                                (request_id,))],
            'households': [dict(r) for r in many(
                db, 'SELECT * FROM ride_household_report WHERE request_id=? ORDER BY household_id',
                (request_id,))],
            'closures': [dict(r) for r in many(db, 'SELECT * FROM road_closure ORDER BY closed_at,id')],
            'logic': ('seats, wheelchair places and driver clock checked per physical segment; '
                      'a seat is only released by a two-witness alighting; '
                      'housing must be delivered before a seat is confirmed')}


