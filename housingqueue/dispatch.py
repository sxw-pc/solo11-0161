"""Offline outage dispatch: battery-backed time-sliced supply decisions.

Hard rules:
* verified life-support devices get continuous power at their declared minimum;
* ordinary loads are shed only with the household's written interruption consent;
* inverter instantaneous limit must hold inside every 5-minute slice, not just
  on the hourly kWh total;
* battery state is carried continuously across switching time; a once-executed
  segment is immutable and later signings/meter data can only affect segments
  whose start time has not yet executed;
* ordinary loads, once energized, run a full minimum-hold (30 minutes) block.
"""
from datetime import datetime, timedelta
import json

from .store import one, many, log

MIN_HOLD_MINUTES = 30
WS_PER_WH = 3600


def _dt(value):
    return datetime.fromisoformat(value)


def _iso(value):
    return value.isoformat(timespec='seconds')


def _witness_evidence(e):
    witness = e.get('witness')
    evidence = e.get('evidence')
    if not witness or not evidence:
        raise ValueError('named witness and evidence reference required')
    return witness, evidence


# ---------------------------------------------------------------- facts intake

def battery(db, e):
    if int(e['usable_wh']) <= 0:
        raise ValueError('battery usable energy must be positive')
    db.execute('INSERT INTO battery(id,community,usable_ws,state,reason) VALUES(?,?,?,?,?)',
               (e['id'], e['community'], int(e['usable_wh']) * WS_PER_WH,
                'available', 'battery usable energy declared'))
    log(db, 'dispatch', e['id'], 'available', 'battery declared', e['id'])
    return 'available'


def inverter(db, e):
    if int(e['limit_w']) <= 0:
        raise ValueError('inverter instantaneous limit must be positive')
    db.execute('INSERT INTO inverter(id,community,limit_w,state,reason) VALUES(?,?,?,?,?)',
               (e['id'], e['community'], int(e['limit_w']),
                'available', 'inverter instantaneous limit declared'))
    log(db, 'dispatch', e['id'], 'available', 'inverter declared', e['id'])
    return 'available'


def grid(db, e):
    if e['state'] not in ('on', 'off'):
        raise ValueError("grid state must be 'on' or 'off'")
    db.execute('INSERT INTO grid_event(id,community,at,state,reason) VALUES(?,?,?,?,?)',
               (e['id'], e['community'], e['at'], e['state'], e.get('reason', 'grid state reported')))
    log(db, 'dispatch', e['id'], e['state'], 'grid connectivity event', e['id'])
    return e['state']


def device(db, e):
    unit = one(db, 'SELECT id,community FROM unit WHERE id=?', (e['unit_id'],))
    if not unit:
        raise ValueError('known unit required for device declaration')
    if int(e['power_w']) <= 0:
        raise ValueError('device minimum power must be positive')
    if _dt(e['window_end']) <= _dt(e['window_start']):
        raise ValueError('device service window must end after it starts')
    critical = bool(e.get('critical'))
    interruptible = bool(e.get('interruptible'))
    consent = e.get('consent', 'none')
    if not critical and interruptible and consent not in ('written',):
        raise ValueError('interruptible ordinary load requires written consent')
    if critical and interruptible:
        raise ValueError('life-support device cannot be declared interruptible')
    if critical:
        state, reason = 'verified', 'verified life-support load: continuous supply committed'
    else:
        state, reason = 'declared', 'ordinary load declared; shedding needs written consent'
    db.execute('INSERT INTO device(id,unit_id,label,critical,power_w,interruptible,consent,window_start,window_end,state,reason)'
               ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (e['id'], e['unit_id'], e['label'], int(critical), int(e['power_w']),
                int(interruptible), consent, e['window_start'], e['window_end'], state, reason))
    log(db, 'dispatch', e['id'], state, reason, e['id'])
    return state


def delivery(db, e):
    """Physical handover of a signed unit; only delivered units draw power."""
    offer = one(db, 'SELECT * FROM offer WHERE id=?', (e['offer_id'],))
    if not offer:
        raise ValueError('unknown offer')
    if offer['state'] != 'signed':
        raise ValueError('physical delivery requires a signed allocation')
    witness, evidence = _witness_evidence(e)
    if one(db, 'SELECT id FROM delivery WHERE offer_id=?', (e['offer_id'],)):
        raise ValueError('unit already physically delivered')
    db.execute('INSERT INTO delivery(id,offer_id,unit_id,household_id,at,witness,evidence,state)'
               ' VALUES(?,?,?,?,?,?,?,?)',
               (e['id'], offer['id'], offer['unit_id'], offer['household_id'],
                e['at'], witness, evidence, 'delivered'))
    db.execute("UPDATE unit SET state='occupied' WHERE id=?", (offer['unit_id'],))
    log(db, 'dispatch', offer['unit_id'], 'occupied', 'signed unit physically delivered and witnessed', e['id'])
    _refresh_pending_plans(db, offer['unit_id'], e['at'], e['id'])
    return 'delivered'


def switch(db, e):
    """Field witness evidence that one planned segment actually switched."""
    plan = one(db, 'SELECT * FROM supply_plan WHERE id=?', (e['plan_id'],))
    if not plan:
        raise ValueError('unknown supply plan')
    seg = one(db, 'SELECT * FROM supply_segment WHERE plan_id=? AND segment_no=?',
              (e['plan_id'], int(e['segment_no'])))
    if not seg:
        raise ValueError('unknown supply segment')
    witness, evidence = _witness_evidence(e)
    if one(db, 'SELECT id FROM switch_witness WHERE plan_id=? AND segment_no=?',
           (e['plan_id'], int(e['segment_no']))):
        raise ValueError('segment switch already witnessed')
    db.execute('INSERT INTO switch_witness(id,plan_id,segment_no,at,witness,evidence) VALUES(?,?,?,?,?,?)',
               (e['id'], e['plan_id'], int(e['segment_no']), e['at'], witness, evidence))
    db.execute('UPDATE supply_segment SET state=?, witness_id=? WHERE plan_id=? AND segment_no=?',
               ('executed', e['id'], e['plan_id'], int(e['segment_no'])))
    log(db, 'dispatch', e['plan_id'], 'executed',
        'segment %s field switch witnessed' % e['segment_no'], e['id'])
    return 'executed'


def meter_conflict(db, e):
    """Device declaration vs meter reading conflict: flag, never treat gap as zero."""
    device_row = one(db, 'SELECT * FROM device WHERE id=?', (e['device_id'],))
    if not device_row:
        raise ValueError('unknown device')
    reading = one(db, 'SELECT * FROM power_reading WHERE id=?', (e['meter_reading_id'],))
    if not reading:
        raise ValueError('unknown meter reading; missing measurement is not zero')
    state = 'conflict_pending'
    db.execute("UPDATE device SET state=?,reason=? WHERE id=?",
               (state, 'meter/declaration mismatch pending verification: %s' % e.get('note', 'unspecified'), e['device_id']))
    log(db, 'dispatch', e['device_id'], state, 'device-meter conflict flagged for verification', e['id'])
    return state


# ------------------------------------------------------------- demand rebuild

def occupied_units(db, community):
    """Signed-but-undelivered units are not occupied loads."""
    return [r['id'] for r in many(db,
        "SELECT DISTINCT d.unit_id AS id FROM delivery d JOIN unit u ON u.id=d.unit_id "
        "WHERE u.community=? AND d.state='delivered'", (community,))]


def recompute_demand(db, community, event_id=None):
    """Rebuild community demand from currently occupied units' latest readings."""
    total = count = 0
    for unit_id in occupied_units(db, community):
        latest = one(db, 'SELECT kw_tenths FROM power_reading WHERE unit_id=? ORDER BY at DESC,id DESC LIMIT 1',
                     (unit_id,))
        if latest:
            total += latest['kw_tenths']
            count += 1
    state = 'measured' if count else 'pending'
    db.execute('INSERT INTO power_demand(community,total_kw_tenths,reading_count,state) VALUES(?,?,?,?) '
               'ON CONFLICT(community) DO UPDATE SET total_kw_tenths=excluded.total_kw_tenths,'
               'reading_count=excluded.reading_count,state=excluded.state',
               (community, total, count, state))
    log(db, 'power', community, state, 'recomputed over delivered occupied units only', event_id)
    return state


# ----------------------------------------------------------------- scheduling

def _load_context(db, community, window_id):
    window = one(db, 'SELECT * FROM dispatch_window WHERE id=?', (window_id,))
    if not window:
        raise ValueError('unknown dispatch window')
    battery_row = one(db, 'SELECT * FROM battery WHERE community=? ORDER BY rowid DESC LIMIT 1', (community,))
    inverter_row = one(db, 'SELECT * FROM inverter WHERE community=? ORDER BY rowid DESC LIMIT 1', (community,))
    if not battery_row or not inverter_row:
        raise ValueError('battery and inverter declarations required before dispatch')
    occupied = set(occupied_units(db, community))
    devices = []
    for d in many(db, "SELECT * FROM device WHERE state='verified' OR state='declared' ORDER BY rowid", ()):
        unit = one(db, 'SELECT community FROM unit WHERE id=?', (d['unit_id'],))
        if unit and unit['community'] == community and d['unit_id'] in occupied:
            devices.append(dict(d))
    return window, battery_row, inverter_row, devices


def _build_schedule(db, window, battery_row, inverter_row, devices):
    start = _dt(window['start_at'])
    end = _dt(window['end_at'])
    step = timedelta(minutes=int(window['slice_minutes']))
    slices = []
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        slices.append((cur, nxt))
        cur = nxt

    critical = [d for d in devices if d['critical']]
    ordinary = [d for d in devices if not d['critical']]

    def active(d, t0, t1):
        ws, we = _dt(d['window_start']), _dt(d['window_end'])
        return ws <= t0 and t1 <= we

    crit_base = sum(d['power_w'] for d in critical)
    if crit_base > inverter_row['limit_w']:
        raise ValueError('life-support load exceeds inverter limit: no feasible dispatch')
    # continuous energy the critical devices need for the whole outage
    crit_energy = crit_base * int((end - start).total_seconds())
    if crit_energy > battery_row['usable_ws']:
        raise ValueError('battery cannot sustain life-support devices through the outage')

    # Candidate ordinary blocks: each interruptible ordinary device may receive
    # one contiguous minimum-hold block starting at a slice boundary inside its
    # service window. Enumerate feasible (device, start_slice, n_slices) blocks.
    blocks = []
    for idx, d in enumerate(ordinary):
        ws, we = _dt(d['window_start']), _dt(d['window_end'])
        for s in range(len(slices)):
            t0 = slices[s][0]
            if t0 < ws:
                continue
            n_hold = MIN_HOLD_MINUTES // int(window['slice_minutes'])
            if s + n_hold > len(slices) or slices[min(s + n_hold, len(slices)) - 1][1] > we:
                continue
            blocks.append((idx, s, n_hold))

    feasible = []
    chosen_energy_need = {}

    def energy_for(choice):
        total_ws = crit_energy
        for idx, s, n in choice:
            total_ws += ordinary[idx]['power_w'] * int(
                (slices[s + n - 1][1] - slices[s][0]).total_seconds())
        return total_ws

    # A choice is a set of non-overlapping blocks (one per device at most).
    def search(choice, bstart):
        energy = energy_for(choice)
        if energy <= battery_row['usable_ws']:
            # verify instantaneous limit on every slice
            ok = True
            for si, (t0, t1) in enumerate(slices):
                p = crit_base
                for idx, s, n in choice:
                    if s <= si < s + n and active(ordinary[idx], t0, t1):
                        p += ordinary[idx]['power_w']
                if p > inverter_row['limit_w']:
                    ok = False
                    break
            if ok:
                key = tuple(sorted((idx, s) for idx, s, n in choice))
                if key not in chosen_energy_need:
                    chosen_energy_need[key] = (list(choice), energy)
        for bi in range(bstart, len(blocks)):
            idx, s, n = blocks[bi]
            if any(c[0] == idx for c in choice):
                continue
            if any(not (s + n <= c[1] or c[1] + c[2] <= s) for c in choice):
                continue
            search(choice + [blocks[bi]], bi + 1)

    search([], 0)
    # retain only maximal service choices (no choice whose powered set is a
    # strict subset of another feasible one) and order by insertion discovery,
    # never by household/unit id
    candidates = list(chosen_energy_need.values())
    maximal = []
    for choice, energy in candidates:
        served = {idx for idx, s, n in choice}
        if any(served < {i for i, s2, n2 in other} for other, _ in candidates):
            continue
        maximal.append((choice, energy))
    if not maximal:
        raise ValueError('no feasible supply sequence satisfies hard constraints')
    return slices, critical, ordinary, maximal


def _persist_plan(db, window, plan_id, seq, adopted, slices, critical, ordinary,
                  choice, battery_row, inverter_row, devices_by_key, state):
    db.execute('INSERT INTO supply_plan(id,community,window_id,seq,adopted,state,reason) VALUES(?,?,?,?,?,?,?)',
               (plan_id, window['community'], window['id'], seq, int(adopted), state,
                'feasible time-sliced battery dispatch'))
    powered_by_slice = _powered_by_slice(slices, critical, ordinary, choice)
    return _write_segments(db, plan_id, slices, 0, battery_row['usable_ws'], powered_by_slice,
                           inverter_row, list(devices_by_key.values()), state)


def plan_window(db, e):
    window = one(db, 'SELECT * FROM dispatch_window WHERE id=?', (e['window_id'],)) if e.get('window_id') else None
    if not window:
        if _dt(e['end_at']) <= _dt(e['start_at']):
            raise ValueError('dispatch window must end after it starts')
        if int(e['slice_minutes']) <= 0 or MIN_HOLD_MINUTES % int(e['slice_minutes']):
            raise ValueError('slice minutes must divide the %d minute minimum hold' % MIN_HOLD_MINUTES)
        db.execute('INSERT INTO dispatch_window(id,community,start_at,end_at,slice_minutes,adopt_seq,state,reason)'
                   ' VALUES(?,?,?,?,?,?,?,?)',
                   (e['id'], e['community'], e['start_at'], e['end_at'],
                    int(e['slice_minutes']), int(e.get('adopt_seq', 1)), 'planned',
                    'outage dispatch window opened'))
        window = one(db, 'SELECT * FROM dispatch_window WHERE id=?', (e['id'],))
    win, battery_row, inverter_row, devices = _load_context(db, e['community'], window['id'])
    slices, critical, ordinary, maximal = _build_schedule(db, win, battery_row, inverter_row, devices)
    devices_by_key = {d['id']: d for d in devices}
    adopt_seq = int(window['adopt_seq'])
    # full rebuild path: only windows without executed segments reach here, so
    # removing every prior plan and its segments cannot touch witnessed history
    for old in many(db, 'SELECT id FROM supply_plan WHERE window_id=?', (window['id'],)):
        if one(db, "SELECT id FROM supply_segment WHERE plan_id=? AND state='executed' LIMIT 1",
               (old['id'],)):
            raise ValueError('window has executed segments and cannot be rebuilt wholesale')
        db.execute('DELETE FROM supply_segment WHERE plan_id=?', (old['id'],))
        db.execute('DELETE FROM supply_plan WHERE id=?', (old['id'],))
    saved = []
    for seq, (choice, energy) in enumerate(maximal, 1):
        plan_id = '%s-P%d' % (window['id'], seq)
        adopted = seq == adopt_seq
        state = 'planned'
        leftover = _persist_plan(db, win, plan_id, seq, adopted, slices, critical, ordinary,
                                 choice, battery_row, inverter_row, devices_by_key, state)
        saved.append({'plan_id': plan_id, 'adopted': adopted, 'battery_left_ws': leftover})
        log(db, 'dispatch', plan_id, 'planned',
            'feasible sequence %d stored%s' % (seq, ' and adopted' if adopted else ''), e.get('event_id'))
    if not any(s['adopted'] for s in saved):
        raise ValueError('adopt_seq %d does not match any feasible sequence' % adopt_seq)
    return saved


def open_window(db, e):
    return plan_window(db, e)


def adopt(db, e):
    plan = one(db, 'SELECT * FROM supply_plan WHERE id=?', (e['plan_id'],))
    if not plan:
        raise ValueError('unknown supply plan')
    if one(db, "SELECT id FROM supply_segment WHERE plan_id=? AND state='executed' LIMIT 1", (e['plan_id'],)):
        raise ValueError('plan already has executed segments and cannot be replaced')
    db.execute('UPDATE supply_plan SET adopted=0 WHERE window_id=?', (plan['window_id'],))
    db.execute('UPDATE supply_plan SET adopted=1 WHERE id=?', (e['plan_id'],))
    db.execute('UPDATE dispatch_window SET adopt_seq=? WHERE id=?', (plan['seq'], plan['window_id']))
    log(db, 'dispatch', e['plan_id'], 'adopted', 'dispatcher adopted feasible sequence', e['id'])
    return 'adopted'


def _write_segments(db, plan_id, slices, start_seg_no, battery_start, powered_by_slice,
                    inverter_row, all_devices, state):
    """Persist segments for a slice range, carrying battery state continuously."""
    battery_left = battery_start
    for offset, (t0, t1) in enumerate(slices):
        powered = powered_by_slice[start_seg_no + offset]
        power_w = sum(d['power_w'] for d in powered)
        if power_w > inverter_row['limit_w']:
            return None
        before = battery_left
        battery_left -= power_w * int((t1 - t0).total_seconds())
        if battery_left < 0:
            return None
        fulfilled = {d['id'] for d in powered}
        unfulfilled = []
        for d in all_devices:
            ws, we = _dt(d['window_start']), _dt(d['window_end'])
            if d['id'] not in fulfilled and ws <= t0 and t1 <= we:
                unfulfilled.append({'device_id': d['id'], 'unit_id': d['unit_id'],
                                    'label': d['label'],
                                    'reason': 'life-support protected; ordinary load shed under written consent'
                                    if not d['critical'] else 'life-support commitment unmet'})
        db.execute('INSERT INTO supply_segment(plan_id,segment_no,start_at,end_at,battery_before_ws,'
                   'battery_after_ws,power_w,limit_w,powered_devices,unfulfilled,state,witness_id)'
                   ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (plan_id, start_seg_no + offset, _iso(t0), _iso(t1), before, battery_left,
                    power_w, inverter_row['limit_w'],
                    json.dumps([d['id'] for d in powered], sort_keys=True),
                    json.dumps(unfulfilled, sort_keys=True), state, None))
    return battery_left


def _powered_by_slice(slices, critical, ordinary, choice):
    """Map every slice number to the powered device list for a choice."""
    by_slice = {}
    for seg_no, (t0, t1) in enumerate(slices):
        powered = [dict(d) for d in critical]
        for idx, s, n in choice:
            if s <= seg_no < s + n:
                powered.append(dict(ordinary[idx]))
        by_slice[seg_no] = powered
    return by_slice


def _refresh_pending_plans(db, unit_id, at, event_id, recompute=True):
    """Recompute demand and rebuild only segments not yet executed.

    Executed segments are immutable: later signings, deliveries and meter data
    never overwrite them. Battery state carries across the execution boundary.
    """
    unit = one(db, 'SELECT community FROM unit WHERE id=?', (unit_id,))
    if not unit:
        return
    community = unit['community']
    if recompute:
        recompute_demand(db, community, event_id)
    for window in many(db, 'SELECT * FROM dispatch_window WHERE community=? ORDER BY rowid', (community,)):
        executed = many(db, "SELECT ps.* FROM supply_segment ps JOIN supply_plan pp ON pp.id=ps.plan_id "
                            "WHERE pp.window_id=? AND ps.state='executed' ORDER BY ps.segment_no", (window['id'],))
        if not executed:
            # nothing executed yet: a full rebuild is safe and equivalent
            plan_window(db, {'id': window['id'], 'window_id': window['id'], 'community': community,
                             'event_id': event_id})
            continue
        cutoff = executed[-1]['end_at']
        if _dt(at) < _dt(cutoff):
            # the event belongs to the already-executed past; history is frozen
            log(db, 'dispatch', window['id'], 'history_locked',
                'executed segments retained; event at %s cannot rewrite them' % at, event_id)
            continue
        win, battery_row, inverter_row, devices = _load_context(db, community, window['id'])
        slices, critical, ordinary, maximal = _build_schedule(db, win, battery_row, inverter_row, devices)
        existing = {p['seq']: p for p in many(db, "SELECT * FROM supply_plan WHERE window_id=?", (window['id'],))}
        for seq, (choice, _energy) in enumerate(maximal, 1):
            plan_id = '%s-P%d' % (window['id'], seq)
            powered_by_slice = _powered_by_slice(slices, critical, ordinary, choice)
            if seq not in existing:
                # a newly feasible alternative timeline: record it in full
                _persist_plan(db, win, plan_id, seq, seq == int(window['adopt_seq']), slices,
                              critical, ordinary, choice, battery_row, inverter_row,
                              {d['id']: d for d in devices}, 'planned')
                log(db, 'dispatch', plan_id, 'planned',
                    'newly feasible alternative sequence recorded after occupancy change', event_id)
                continue
            plan = existing[seq]
            # the re-enumerated future must not contradict witnessed history
            compatible = True
            for seg in many(db, "SELECT * FROM supply_segment WHERE plan_id=? AND state='executed' ORDER BY segment_no",
                            (plan['id'],)):
                if {d['id'] for d in powered_by_slice[seg['segment_no']]} != set(json.loads(seg['powered_devices'])):
                    compatible = False
                    break
            if not compatible:
                db.execute("UPDATE supply_plan SET state='replanning_required',reason=? WHERE id=?",
                           ('new occupancy conflicts with witnessed switches', plan['id']))
                log(db, 'dispatch', plan_id, 'replanning_required',
                    'pending choice conflicts with executed segments; dispatcher must re-adopt', event_id)
                continue
            boundary = one(db, "SELECT * FROM supply_segment WHERE plan_id=? AND state='executed' "
                               "ORDER BY segment_no DESC LIMIT 1", (plan['id'],))
            if not boundary:
                # plan recorded but no switch witnessed yet: safe full rewrite
                db.execute('DELETE FROM supply_segment WHERE plan_id=?', (plan['id'],))
                _write_segments(db, plan['id'], slices, 0, battery_row['usable_ws'],
                                powered_by_slice, inverter_row, devices, 'planned')
                log(db, 'dispatch', plan_id, 'replanned',
                    'full plan rebuilt after occupancy change; no segment had executed', event_id)
                continue
            db.execute("DELETE FROM supply_segment WHERE plan_id=? AND state='planned'", (plan['id'],))
            future_slices = [(t0, t1) for seg_no, (t0, t1) in enumerate(slices)
                             if seg_no > boundary['segment_no']]
            leftover = _write_segments(db, plan_id, future_slices, boundary['segment_no'] + 1,
                                       boundary['battery_after_ws'], powered_by_slice, inverter_row,
                                       devices, 'planned')
            if leftover is None:
                db.execute("UPDATE supply_plan SET state='replanning_required',reason=? WHERE id=?",
                           ('pending segments exceed inverter or battery after occupancy change', plan['id']))
                log(db, 'dispatch', plan_id, 'replanning_required',
                    'hard constraint violated on pending segments; re-adoption required', event_id)
            else:
                log(db, 'dispatch', plan_id, 'replanned',
                    'pending segments rebuilt; executed segments and battery boundary preserved', event_id)
        # plans that no longer correspond to any feasible choice cannot stand
        for seq, plan in existing.items():
            if seq > len(maximal):
                db.execute("DELETE FROM supply_segment WHERE plan_id=? AND state='planned'", (plan['id'],))
                db.execute("UPDATE supply_plan SET state='replanning_required',reason=? WHERE id=?",
                           ('occupancy change removed this feasible sequence', plan['id']))
                log(db, 'dispatch', plan['id'], 'replanning_required',
                    'sequence no longer feasible after occupancy change', event_id)
        log(db, 'dispatch', window['id'], 'replanned',
            'pending segments rebuilt after occupancy change; executed segments immutable', event_id)


def refresh_after_occupancy(db, unit_id, at, event_id):
    """A signing changes occupancy inputs; only pending segments may change."""
    _refresh_pending_plans(db, unit_id, at, event_id, recompute=False)


def refresh_after_meter(db, unit_id, at, event_id):
    """Late meter readings refresh pending supply segments, never executed ones."""
    _refresh_pending_plans(db, unit_id, at, event_id, recompute=False)
