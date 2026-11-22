"""Building tank mixing, pipe retention, valve/pump routing and drinking directives.

The hydraulic model is event-sourced like the other ledgers: import events only
register network nodes, approved valve/pump actions and laboratory samples.
Every handler rebuilds the derived minute-by-minute simulation, parcel
snapshots and household directives, so an audit replay reaches byte-identical
derived state.

Time is discrete one-minute steps. Within each minute endpoints pull demand
upstream first; parcels move FIFO through pipes (a pipe with no incoming water
keeps its retained parcels instead of emptying); only afterwards does each tank
mix confirmed clean inflow into what remains. A sub-threshold simulated tap
value never lifts an order by itself: a later qc-passed resample at that tap is
required.
"""
from .store import one, many, log

ROUND = 6


def _num(e, key, minimum=0.0, allow_none=False):
    if e.get(key) is None:
        if allow_none:
            return None
        raise ValueError(key + ' required')
    v = float(e[key])
    if v < minimum:
        raise ValueError(key + ' below allowed minimum')
    return v


def _minute(e, key):
    v = int(e[key])
    if v < 0:
        raise ValueError('minute offsets cannot be negative')
    return v


def _building(db, e):
    p = one(db, 'SELECT * FROM flow_plan WHERE building=?', (e['building'],))
    if not p:
        raise ValueError('flow plan for building must be registered first')
    return p


def _node(db, node_id):
    return one(db, 'SELECT * FROM flow_node WHERE id=?', (node_id,))


def plan(db, e):
    if one(db, 'SELECT id FROM flow_plan WHERE building=?', (e['building'],)):
        raise ValueError('one flow plan per building')
    horizon = int(e['horizon'])
    threshold = int(e['threshold'])
    if horizon < 1 or threshold <= 0:
        raise ValueError('positive horizon and threshold required')
    db.execute('INSERT INTO flow_plan(id,building,horizon,threshold,title) VALUES(?,?,?,?,?)',
               (e['id'], e['building'], horizon, threshold, e['title']))
    log(db, 'water_flow', e['id'], 'planned', 'flow network plan registered', e['id'])
    simulate(db)
    return 'planned'


def _insert_node(db, e, kind):
    p = _building(db, e)
    if _node(db, e['id']):
        raise ValueError('duplicate flow node id')
    upstream = e.get('upstream_id')
    if upstream is not None and not _node(db, upstream):
        raise ValueError('upstream node must already be registered')
    db.execute(
        'INSERT INTO flow_node(id,building,kind,upstream_id,downstream_id,tap_id,initial_volume,'
        'initial_cfu,capacity,cfu,clean_inflow,pipe_volume,valve_state,demand) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (e['id'], e['building'], kind, upstream, None, e.get('tap_id'),
         float(e.get('initial_volume', 0.0)), float(e.get('initial_cfu', 0.0)),
         float(e['capacity']) if e.get('capacity') is not None else None,
         float(e['cfu']) if e.get('cfu') is not None else None,
         float(e['clean_inflow']) if e.get('clean_inflow') is not None else None,
         float(e['pipe_volume']) if e.get('pipe_volume') is not None else None,
         e.get('valve_state'), float(e['demand']) if e.get('demand') is not None else None))
    return p


def tank(db, e):
    p = _building(db, e)
    if _node(db, e['id']):
        raise ValueError('duplicate flow node id')
    volume, capacity = _num(e, 'initial_volume'), _num(e, 'capacity', minimum=1e-9)
    if volume > capacity:
        raise ValueError('tank cannot start above its capacity')
    cfu, clean = _num(e, 'initial_cfu'), _num(e, 'clean_inflow')
    db.execute(
        'INSERT INTO flow_node(id,building,kind,upstream_id,downstream_id,tap_id,initial_volume,'
        'initial_cfu,capacity,cfu,clean_inflow,pipe_volume,valve_state,demand) '
        "VALUES(?,?, 'tank', NULL,NULL,NULL,?,?,?,?,?,NULL,NULL,NULL)",
        (e['id'], e['building'], volume, cfu, capacity, cfu, clean))
    log(db, 'water_flow', e['id'], 'registered', 'mixing tank registered', e['id'])
    simulate(db)
    return 'registered'


def pump(db, e):
    if e['valve_state'] not in ('on', 'off'):
        raise ValueError("pump initial state must be 'on' or 'off'")
    _num(e, 'capacity', minimum=1e-9)
    _insert_node(db, e, 'pump')
    log(db, 'water_flow', e['id'], 'registered', 'controllable pump registered', e['id'])
    simulate(db)
    return 'registered'


def pipe(db, e):
    p = _building(db, e)
    volume = _num(e, 'pipe_volume', minimum=1e-9)
    initial = _num(e, 'initial_volume') if e.get('initial_volume') is not None else volume
    if initial > volume:
        raise ValueError('retained water cannot exceed pipe volume')
    cfu = _num(e, 'initial_cfu')
    if not _node(db, e['upstream_id']):
        raise ValueError('upstream node must already be registered')
    if _node(db, e['id']):
        raise ValueError('duplicate flow node id')
    db.execute(
        'INSERT INTO flow_node(id,building,kind,upstream_id,downstream_id,tap_id,initial_volume,'
        'initial_cfu,capacity,cfu,clean_inflow,pipe_volume,valve_state,demand) '
        "VALUES(?,?, 'pipe',?,NULL,NULL,?,?,NULL,?,NULL,?,NULL,NULL)",
        (e['id'], e['building'], e['upstream_id'], initial, cfu, cfu, volume))
    log(db, 'water_flow', e['id'], 'registered', 'retention pipe registered', e['id'])
    simulate(db)
    return 'registered'


def valve(db, e):
    if e['valve_state'] not in ('open', 'closed'):
        raise ValueError("valve initial state must be 'open' or 'closed'")
    _insert_node(db, e, 'valve')
    log(db, 'water_flow', e['id'], 'registered', 'controllable valve registered', e['id'])
    simulate(db)
    return 'registered'


def outlet(db, e):
    _num(e, 'demand', minimum=1e-9)
    _insert_node(db, e, 'outlet')
    log(db, 'water_flow', e['id'], 'registered', 'flush outlet registered', e['id'])
    simulate(db)
    return 'registered'


def flow_tap(db, e):
    tap = one(db, 'SELECT * FROM water_tap WHERE id=?', (e['tap_id'],))
    if not tap:
        raise ValueError('flow tap requires a registered water_tap')
    if _node(db, e['tap_id']):
        raise ValueError('water_tap already has a flow node')
    _num(e, 'demand', minimum=1e-9)
    _insert_node(db, e, 'tap')
    log(db, 'water_flow', e['id'], 'registered', 'household tap bound to unit ' + tap['unit_id'], e['id'])
    simulate(db)
    return 'registered'


def action(db, e):
    p = _building(db, e)
    target = _node(db, e['target_id'])
    if not target or target['building'] != e['building']:
        raise ValueError('action target must be a node of the same building')
    minute = _minute(e, 'at_minute')
    if minute >= p['horizon']:
        raise ValueError('action minute outside simulation horizon')
    if target['kind'] == 'valve' and e['state'] not in ('open', 'closed'):
        raise ValueError('valve action must open or close')
    if target['kind'] == 'pump' and e['state'] not in ('on', 'off'):
        raise ValueError('pump action must switch on or off')
    if target['kind'] not in ('valve', 'pump'):
        raise ValueError('only valves and pumps take actions')
    if e.get('target_kind') != target['kind']:
        raise ValueError('action target_kind must match the target node kind')
    approved = 1 if int(bool(e.get('approved'))) else 0
    db.execute('INSERT INTO flow_action(id,building,at_minute,target_id,kind,state,approved,reason) '
               'VALUES(?,?,?,?,?,?,?,?)',
               (e['id'], e['building'], minute, e['target_id'], target['kind'], e['state'],
                approved, e.get('reason', '')))
    log(db, 'water_flow', e['id'], 'approved' if approved else 'rejected_request',
        'action stored; applied to hydraulics only when approved', e['id'])
    simulate(db)
    return 'approved' if approved else 'rejected_request'


def flow_sample(db, e):
    p = _building(db, e)
    tap = _node(db, e['tap_node_id'])
    if not tap or tap['kind'] != 'tap' or tap['building'] != e['building']:
        raise ValueError('flow sample requires a tap node in the same building')
    sampled, received = _minute(e, 'sampled_minute'), _minute(e, 'received_minute')
    if received < sampled:
        raise ValueError('laboratory cannot receive a sample before it was drawn')
    if sampled >= p['horizon']:
        raise ValueError('sample minute outside simulation horizon')
    if e['qc'] not in ('pass', 'fail'):
        raise ValueError("laboratory qc must be 'pass' or 'fail'")
    cfu = _num(e, 'cfu_per_litre')
    threshold = p['threshold']
    state = 'alert' if cfu >= threshold else 'clear'
    note = 'qc failed: sample is non-probative and cannot change a directive' if e['qc'] == 'fail' \
        else ('above threshold: drinking order required' if state == 'alert'
              else 'below threshold: may lift an order only after simulated clearance')
    db.execute('INSERT INTO flow_sample(id,tap_node_id,sampled_minute,received_minute,'
               'cfu_per_litre,qc,threshold,state,note) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'], e['tap_node_id'], sampled, received, cfu, e['qc'], threshold, state, note))
    log(db, 'water_flow', e['id'], state, note, e['id'])
    simulate(db)
    return state


HANDLERS = {
    'flow_plan': plan,
    'flow_tank': tank,
    'flow_pump': pump,
    'flow_pipe': pipe,
    'flow_valve': valve,
    'flow_outlet': outlet,
    'flow_tap': flow_tap,
    'flow_action': action,
    'flow_sample': flow_sample,
}


class _Parcel:
    __slots__ = ('uid', 'born_minute', 'origin', 'volume', 'cfu')

    def __init__(self, uid, born_minute, origin, volume, cfu):
        self.uid = uid
        self.born_minute = born_minute
        self.origin = origin
        self.volume = volume
        self.cfu = cfu

    def split(self, volume, uid):
        self.volume -= volume
        return _Parcel(uid, self.born_minute, self.origin, volume, self.cfu)


class _Engine:
    def __init__(self, db, plan_row):
        self.db = db
        self.plan = plan_row
        self.building = plan_row['building']
        self.horizon = plan_row['horizon']
        self.threshold = plan_row['threshold']
        self.nodes = {n['id']: dict(n) for n in
                      many(db, 'SELECT * FROM flow_node WHERE building=? ORDER BY id', (self.building,))}
        self.actions = [dict(a) for a in many(
            db, 'SELECT * FROM flow_action WHERE building=? AND approved=1 ORDER BY at_minute,id', (self.building,))]
        self.samples = [dict(s) for s in many(
            db, 'SELECT * FROM flow_sample WHERE tap_node_id IN (SELECT id FROM flow_node WHERE building=?) '
            'ORDER BY received_minute,id', (self.building,))]
        self.parcels = {nid: [] for nid, n in self.nodes.items() if n['kind'] == 'pipe'}
        self.seq = 0
        self.acc = {}
        self.minute_rows = []
        self.parcel_rows = []

    def run(self):
        self._seed_pipes()
        upstream_of = {nid: n['upstream_id'] for nid, n in self.nodes.items()}
        endpoints = sorted(nid for nid, n in self.nodes.items()
                           if n['kind'] in ('tap', 'outlet') and nid not in upstream_of.values())
        for minute in range(self.horizon):
            self._apply_actions(minute)
            self.acc = {nid: dict(inflow=0.0, outflow=0.0, mass_in=0.0, mass_out=0.0,
                                  start_volume=self._volume(nid), start_mass=self._mass(nid))
                        for nid in self.nodes}
            for nid in endpoints:
                self._pull(nid, self.nodes[nid]['demand'], minute)
            for nid, n in self.nodes.items():
                if n['kind'] == 'tank':
                    self._mix_tank(nid)
            self._record_minute(minute)
            self._snapshot_parcels(minute)
        return self.minute_rows, self.parcel_rows, self._directives()

    # ---- state helpers ----------------------------------------------------
    def _volume(self, nid):
        n = self.nodes[nid]
        if n['kind'] == 'tank':
            return n['_volume']
        if n['kind'] == 'pipe':
            return sum(p.volume for p in self.parcels[nid])
        return 0.0

    def _mass(self, nid):
        n = self.nodes[nid]
        if n['kind'] == 'tank':
            return n['_volume'] * n['_cfu']
        if n['kind'] == 'pipe':
            return sum(p.volume * p.cfu for p in self.parcels[nid])
        return 0.0

    def _seed_pipes(self):
        for nid, n in self.nodes.items():
            n['_volume'] = n['initial_volume'] or 0.0
            n['_cfu'] = n['initial_cfu']
            if n['kind'] == 'pipe' and n['initial_volume'] > 0:
                self.parcels[nid].append(
                    _Parcel(nid + '-seed', -1, nid, n['initial_volume'], n['initial_cfu']))

    def _apply_actions(self, minute):
        for a in self.actions:
            if a['at_minute'] == minute:
                self.nodes[a['target_id']]['valve_state'] = a['state']

    # ---- hydraulic pull ---------------------------------------------------
    def _pull(self, nid, amount, minute):
        """Return the parcels delivered at node nid when amount L is requested."""
        if amount <= 0:
            return []
        n = self.nodes[nid]
        kind = n['kind']
        if kind == 'valve' and n['valve_state'] == 'closed':
            return []
        if kind == 'pump':
            if n['valve_state'] != 'on':
                return []
            amount = min(amount, n['capacity'])
        if kind in ('tap', 'outlet', 'valve', 'pump'):
            delivered = self._pull(n['upstream_id'], amount, minute)
            volume = sum(p.volume for p in delivered)
            self.acc[nid]['inflow'] += volume
            self.acc[nid]['mass_in'] += sum(p.volume * p.cfu for p in delivered)
            self.acc[nid]['outflow'] += volume
            self.acc[nid]['mass_out'] += sum(p.volume * p.cfu for p in delivered)
            return delivered
        if kind == 'pipe':
            incoming = self._pull(n['upstream_id'], amount, minute)
            in_volume = sum(p.volume for p in incoming)
            self.acc[nid]['inflow'] += in_volume
            self.acc[nid]['mass_in'] += sum(p.volume * p.cfu for p in incoming)
            if in_volume <= 0.0:
                # Stagnation: zero flow keeps every retained parcel in place.
                return []
            self.parcels[nid].extend(incoming)
            want = min(amount, sum(p.volume for p in self.parcels[nid]))
            delivered = []
            remaining = want
            while remaining > 1e-12:
                head = self.parcels[nid][0]
                if head.volume <= remaining + 1e-12:
                    delivered.append(head)
                    remaining -= head.volume
                    self.parcels[nid].pop(0)
                else:
                    self.seq += 1
                    delivered.append(head.split(remaining, head.uid + '~m' + str(minute) + '-' + str(self.seq)))
                    remaining = 0.0
            out_volume = sum(p.volume for p in delivered)
            self.acc[nid]['outflow'] += out_volume
            self.acc[nid]['mass_out'] += sum(p.volume * p.cfu for p in delivered)
            return delivered
        if kind == 'tank':
            take = min(amount, n['_volume'])
            if take <= 0.0:
                return []
            self.seq += 1
            parcel = _Parcel(nid + '-out-m' + str(minute) + '-' + str(self.seq),
                             minute, nid, take, n['_cfu'])
            n['_volume'] -= take
            self.acc[nid]['outflow'] += take
            self.acc[nid]['mass_out'] += take * n['_cfu']
            return [parcel]
        raise ValueError('unknown node kind ' + kind)

    def _mix_tank(self, nid):
        n = self.nodes[nid]
        clean = min(n['clean_inflow'] or 0.0, max(0.0, n['capacity'] - n['_volume']))
        n['_volume'] += clean
        if n['_volume'] > 0:
            n['_cfu'] = (n['_volume'] - clean) * n['_cfu'] / n['_volume']
        self.acc[nid]['inflow'] += clean

    # ---- recording --------------------------------------------------------
    def _record_minute(self, minute):
        for nid, n in self.nodes.items():
            a = self.acc[nid]
            end_volume, end_mass = self._volume(nid), self._mass(nid)
            cfu = None
            if n['kind'] == 'tank':
                cfu = n['_cfu']
            elif n['kind'] == 'pipe' and end_volume > 0:
                cfu = end_mass / end_volume
            elif n['kind'] in ('tap', 'outlet') and a['outflow'] > 0:
                cfu = a['mass_out'] / a['outflow']
            diff = (end_mass - a['start_mass']) - (a['mass_in'] - a['mass_out'])
            self.minute_rows.append((
                self.building, minute, nid, n['kind'], round(end_volume, ROUND),
                None if cfu is None else round(cfu, ROUND),
                round(a['inflow'], ROUND), round(a['outflow'], ROUND),
                round(a['mass_in'], ROUND), round(a['mass_out'], ROUND),
                round(diff, ROUND), 1 if n['valve_state'] == 'on' else 0,
                n['valve_state'] or ''))

    def _snapshot_parcels(self, minute):
        for nid, queue in self.parcels.items():
            for p in queue:
                self.parcel_rows.append((self.building, minute, p.uid, p.born_minute,
                                         p.origin, round(p.volume, ROUND),
                                         round(p.cfu, ROUND), nid))

    # ---- directives -------------------------------------------------------
    def _directives(self):
        from .water import occupant
        rows = []
        state_at = {(r[1], r[2]): r[12] for r in self.minute_rows}
        for nid, n in sorted(self.nodes.items()):
            if n['kind'] != 'tap':
                continue
            tap = one(self.db, 'SELECT unit_id FROM water_tap WHERE id=?', (n['tap_id'],))
            unit_id = tap['unit_id'] if tap else None
            household = occupant(self.db, unit_id) if unit_id else None
            minute_rows = {r[1]: r for r in self.minute_rows if r[2] == nid}
            first_clear = next((m for m in range(self.horizon)
                                if minute_rows[m][7] > 0 and minute_rows[m][5] is not None
                                and minute_rows[m][5] < self.threshold), None)
            advisory, alert_id, lift_id = False, None, None
            blocked_qc, blocked_early = [], []
            for m in range(self.horizon):
                due = [s for s in self.samples
                       if s['tap_node_id'] == nid and s['received_minute'] == m]
                for s in sorted(due, key=lambda x: x['id']):
                    if s['qc'] == 'fail':
                        if advisory:
                            blocked_qc.append(s['id'])
                        continue
                    if s['state'] == 'alert':
                        advisory, alert_id, lift_id = True, s['id'], None
                    elif advisory and first_clear is not None and s['sampled_minute'] >= first_clear:
                        advisory, lift_id = False, s['id']
                    elif advisory:
                        blocked_early.append(s['id'])
                r = minute_rows[m]
                sim_cfu, outflow = r[5], r[7]
                if outflow <= 0.0:
                    directive, supply, basis = self._bottled_basis(nid, m, state_at)
                elif advisory:
                    directive, supply = 'boil', 'delivered'
                    basis = 'advisory active from qc-passed alert sample ' + str(alert_id)
                    if blocked_qc:
                        basis += '; qc-failed sample(s) ' + ','.join(blocked_qc) + ' cannot lift it'
                    if blocked_early:
                        basis += '; clear sample(s) ' + ','.join(blocked_early) \
                            + ' pre-date simulated clearance at minute ' + str(first_clear)
                    basis += '; simulated tap concentration ' + str(sim_cfu)
                elif sim_cfu is not None and sim_cfu >= self.threshold:
                    directive, supply = 'boil', 'delivered'
                    basis = 'simulated tap concentration ' + str(sim_cfu) \
                        + ' >= threshold ' + str(self.threshold)
                else:
                    directive, supply = 'direct', 'delivered'
                    if lift_id:
                        basis = ('simulated concentration ' + str(sim_cfu) + ' < threshold '
                                 + str(self.threshold) + ' and qc-passed resample ' + lift_id
                                 + ' cleared the tap at minute ' + str(m))
                    else:
                        basis = 'simulated concentration ' + str(sim_cfu) \
                            + ' < threshold ' + str(self.threshold)
                if household:
                    target, kind = household, 'household'
                else:
                    target, kind = 'PROPERTY:' + str(unit_id), 'property_vacant'
                rows.append((self.building, m, nid, unit_id, directive,
                             sim_cfu, supply, basis, target, kind))
        return rows

    def _bottled_basis(self, nid, minute, state_at):
        n = self.nodes[nid]
        reason = 'upstream interruption'
        cur = n['upstream_id']
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            u = self.nodes[cur]
            state = state_at.get((minute, cur), u['valve_state'])
            if u['kind'] == 'valve' and state == 'closed':
                reason = 'valve ' + cur + ' closed at minute ' + str(minute)
                break
            if u['kind'] == 'pump' and state != 'on':
                reason = 'pump ' + cur + ' stopped at minute ' + str(minute)
                break
            cur = u['upstream_id']
        return 'bottled', 'interrupted', 'no water delivered: ' + reason + '; bottled water required, zero flow is not treated as clearance'


def simulate(db):
    """Rebuild every derived table from the registered event state."""
    plans = many(db, 'SELECT * FROM flow_plan ORDER BY id')
    for p in plans:
        building = p['building']
        for table in ('flow_minute', 'flow_parcel', 'flow_directive'):
            db.execute('DELETE FROM ' + table + ' WHERE building=?', (building,))
        if not many(db, 'SELECT id FROM flow_node WHERE building=?', (building,)):
            continue
        minute_rows, parcel_rows, directive_rows = _Engine(db, p).run()
        db.executemany('INSERT INTO flow_minute(building,minute,node_id,kind,volume,cfu,inflow,'
                       'outflow,mass_in,mass_out,conservation_diff,pump_on,valve_state) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', minute_rows)
        db.executemany('INSERT INTO flow_parcel(building,minute,parcel_uid,born_minute,origin,'
                       'volume,cfu,location) VALUES(?,?,?,?,?,?,?,?)', parcel_rows)
        db.executemany('INSERT INTO flow_directive(building,minute,tap_node_id,unit_id,directive,'
                       'sim_cfu,supply,basis,notify_target,notify_kind) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       directive_rows)
    worst = many(db, 'SELECT building, MAX(ABS(conservation_diff)) AS d FROM flow_minute GROUP BY building')
    return [{'building': w['building'], 'max_abs_diff': w['d'] or 0.0} for w in worst]
