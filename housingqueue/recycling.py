"""离线回收物料流台账。

从门店封签装箱、去皮复称、容器拆合、整车出站、加工厂双边接收到分拣残渣、
实际再生物与能源回收证书，每段转移都按整数克记账，全流程满足质量守恒：
同一投入不会在两个下游容器重复出现；去皮、蒸发、残渣分别有独立依据；
没有加工厂接收的出站单不构成已回收；住户积分只基于可归属的实际再生物质量。
"""
from .store import one, many, log
from . import reward

STREAMS = ('paper', 'plastic', 'metal')
ROUTES = ('recycling', 'energy')
# 容器生命周期：存桶 -> 封签 -> 去皮 -> 出站 -> 厂方接收 -> 处置腾空（或被合并）
CONTAINER_STATES = ('sealed', 'tared', 'shipped', 'received', 'emptied', 'merged')


def _int(e, key):
    return int(e[key])


def _container(db, cid):
    c = one(db, 'SELECT * FROM recycling_container WHERE id=?', (cid,))
    if not c:
        raise ValueError('unknown sealed container')
    return c


def _require_state(c, *states):
    if c['state'] not in states:
        raise ValueError(f"container {c['id']} state {c['state']} not in {states}")


def _post(db, eid, cid, household_id, stream, change, basis, drop_id=None, reason=''):
    db.execute(
        'INSERT INTO recycling_lot_ledger(event_id,container_id,household_id,stream,waste_drop_id,'
        'change_grams,basis,reason) VALUES(?,?,?,?,?,?,?,?)',
        (eid, cid, household_id, stream, drop_id, int(change), basis, reason))


def _slices(db, cid):
    """容器当前有效（正余额）物料切片，按住户、品类、投递单稳定排序。"""
    rows = many(db,
                'SELECT household_id,stream,waste_drop_id,SUM(change_grams) AS grams '
                'FROM recycling_lot_ledger WHERE container_id=? GROUP BY household_id,stream,waste_drop_id '
                'HAVING grams>0 ORDER BY household_id,stream,waste_drop_id', (cid,))
    return [dict(r) for r in rows]


def _container_grams(db, cid):
    r = one(db, 'SELECT COALESCE(SUM(change_grams),0) AS n FROM recycling_lot_ledger WHERE container_id=?',
            (cid,))
    return r['n']


def _allocate(amount, weights):
    """按权重整数分摊（最大余数法，同票按顺序），保证总量精确守恒。"""
    amount = int(amount)
    if amount < 0:
        raise ValueError('negative allocation forbidden')
    n = len(weights)
    if amount == 0:
        return [0] * n
    total = sum(weights)
    if total <= 0:
        raise ValueError('positive allocation needs positive basis weights')
    floors = [amount * w // total for w in weights]
    leftover = amount - sum(floors)
    if leftover:
        order = sorted(range(n), key=lambda i: (-(amount * weights[i] % total), i))
        for i in order[:leftover]:
            floors[i] += 1
    return floors


def _move_slices(db, eid, src, dst, weights, amount, basis, reason):
    """从源容器各切片按权重把 amount 克迁到下游容器，切片逐笔守恒。"""
    parts = _allocate(amount, weights)
    for sl, part in zip(_slices(db, src), parts):
        if part <= 0:
            continue
        if part > sl['grams']:
            raise ValueError('allocation exceeds slice mass, conservation broken')
        _post(db, eid, src, sl['household_id'], sl['stream'], -part, basis, sl['waste_drop_id'], reason)
        _post(db, eid, dst, sl['household_id'], sl['stream'], part, basis, sl['waste_drop_id'], reason)


# ---------------------------------------------------------------- 容器与投递
def container_seal(db, e):
    if _int(e, 'tare_grams') < 0:
        raise ValueError('tare must be nonnegative')
    old = one(db, 'SELECT id FROM recycling_container WHERE id=?', (e['id'],))
    if old:
        raise ValueError('container seal id already used')
    db.execute('INSERT INTO recycling_container(id,state,sealed_at,tare_grams,note) VALUES(?,?,?,?,?)',
               (e['id'], 'sealed', e['sealed_at'], _int(e, 'tare_grams'), e.get('note', '')))
    log(db, 'recycling', e['id'], 'sealed', 'empty container tare registered and sealed', e['id'])
    return 'sealed'


def drop_pack(db, e):
    """投递装箱：入站毛重记录不变，装箱量按逐笔投递单归因到封签容器。"""
    c = _container(db, e['container_id'])
    _require_state(c, 'sealed')
    if _int(e, 'packed_grams') <= 0:
        raise ValueError('packed mass must be positive')
    drop = one(db, 'SELECT * FROM waste_drop WHERE id=?', (e['waste_drop_id'],))
    if not drop:
        raise ValueError('packing requires an inbound waste_drop; gross weighing is never rewritten')
    if e['stream'] != drop['stream'] or e['household_id'] != drop['household_id']:
        raise ValueError('packed slice must match the inbound deposit')
    already = one(db, 'SELECT COALESCE(SUM(change_grams),0) AS n FROM recycling_lot_ledger '
                     'WHERE waste_drop_id=? AND change_grams>0', (drop['id'],))['n']
    if already + _int(e, 'packed_grams') > drop['mass_grams']:
        raise ValueError('same deposit cannot be packed into two containers beyond its gross mass')
    _post(db, e['id'], c['id'], drop['household_id'], drop['stream'], _int(e, 'packed_grams'),
          'gross_pack', drop['id'], 'packed under seal, inbound gross unchanged')
    log(db, 'recycling', c['id'], 'sealed', 'deposit packed into sealed container', e['id'])
    return 'sealed'


def container_tare(db, e):
    """去皮复称：毛重减皮重得净重；去皮差异按实际切片分摊，须有复称依据。"""
    c = _container(db, e['container_id'])
    _require_state(c, 'sealed')
    gross, tare = _int(e, 'gross_grams'), c['tare_grams']
    net = gross - tare
    current = _container_grams(db, c['id'])
    if net < 0:
        raise ValueError('negative net weight forbidden')
    if net > current:
        raise ValueError('tared net exceeds packed gross without an inbound basis')
    if not e.get('at') or not e.get('weigher'):
        raise ValueError('tare re-weigh needs time and weigher evidence')
    diff = current - net
    if diff:
        slices = _slices(db, c['id'])
        for sl, part in zip(slices, _allocate(diff, [s['grams'] for s in slices])):
            if part:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -part,
                      'tare_difference', sl['waste_drop_id'], f"tare re-weigh by {e['weigher']}")
    db.execute('UPDATE recycling_container SET state=?,gross_grams=?,net_grams=? WHERE id=?',
               ('tared', gross, net, c['id']))
    log(db, 'recycling', c['id'], 'tared',
        f'net {net}g after tare re-weigh; tare difference {diff}g separately evidenced', e['id'])
    return 'tared'


def evaporation_loss(db, e):
    """蒸发/风干损耗：独立依据、逐切片分摊，不允许负重量。"""
    c = _container(db, e['container_id'])
    _require_state(c, 'sealed', 'tared')
    grams = _int(e, 'loss_grams')
    if grams <= 0 or not e.get('reason') or not e.get('inspector'):
        raise ValueError('evaporation loss needs positive mass, reason and inspector')
    slices = _slices(db, c['id'])
    if grams > sum(s['grams'] for s in slices):
        raise ValueError('evaporation loss exceeds container mass')
    for sl, part in zip(slices, _allocate(grams, [s['grams'] for s in slices])):
        if part:
            _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -part,
                  'evaporation_loss', sl['waste_drop_id'], f"{e['reason']} ({e['inspector']})")
    log(db, 'recycling', c['id'], c['state'], f'evaporation loss {grams}g separately evidenced', e['id'])
    return c['state']


# ---------------------------------------------------------------- 容器拆合
def container_split(db, e):
    c = _container(db, e['container_id'])
    _require_state(c, 'sealed', 'tared')
    child = _container(db, e['new_container_id'])
    _require_state(child, 'sealed')
    if _container_grams(db, child['id']):
        raise ValueError('split target must be an empty sealed container')
    grams = _int(e, 'split_grams')
    current = _container_grams(db, c['id'])
    if grams <= 0 or grams > current:
        raise ValueError('split mass must stay inside the source container')
    slices = _slices(db, c['id'])
    _move_slices(db, e['id'], c['id'], child['id'], [s['grams'] for s in slices], grams,
                 'container_split', e.get('reason', 'container split under seal'))
    db.execute('UPDATE recycling_container SET state=? WHERE id=?', (c['state'], child['id']))
    log(db, 'recycling', c['id'], c['state'],
        f'{grams}g split into {child["id"]} without double counting', e['id'])
    log(db, 'recycling', child['id'], c['state'], f'received {grams}g split from {c["id"]}', e['id'])
    return c['state']


def container_merge(db, e):
    target = _container(db, e['target_container_id'])
    _require_state(target, 'sealed', 'tared')
    moved = 0
    for cid in e['source_container_ids']:
        src = _container(db, cid)
        _require_state(src, 'sealed', 'tared')
        if src['id'] == target['id']:
            raise ValueError('container cannot merge into itself')
        slices = _slices(db, src['id'])
        total = sum(s['grams'] for s in slices)
        if total <= 0:
            raise ValueError('only nonempty containers may be merged')
        _move_slices(db, e['id'], src['id'], target['id'], [s['grams'] for s in slices], total,
                     'container_merge', e.get('reason', 'containers merged under seal'))
        db.execute('UPDATE recycling_container SET state=? WHERE id=?', ('merged', src['id']))
        moved += total
    log(db, 'recycling', target['id'], target['state'],
        f'{moved}g merged from {len(e["source_container_ids"])} container(s)', e['id'])
    return target['state']


# ---------------------------------------------------------------- 出站与厂方接收
def shipment_out(db, e):
    """门店出站单：仅代表在途。没有加工厂双边接收永远不能记为已回收。"""
    if one(db, 'SELECT id FROM recycling_shipment WHERE id=?', (e['id'],)):
        raise ValueError('shipment id already used')
    ids = e['container_ids']
    if not ids:
        raise ValueError('shipment needs at least one container')
    total = 0
    for cid in ids:
        c = _container(db, cid)
        _require_state(c, 'tared')
        grams = _container_grams(db, cid)
        if grams <= 0:
            raise ValueError('empty container cannot be shipped')
        total += grams
        db.execute("UPDATE recycling_container SET state='shipped' WHERE id=?", (cid,))
    db.execute("INSERT INTO recycling_shipment(id,at,state,note) VALUES(?,?,'in_transit',?)",
               (e['id'], e['at'], e.get('note', '')))
    for cid in ids:
        db.execute('INSERT INTO recycling_shipment_container(shipment_id,container_id) VALUES(?,?)',
                   (e['id'], cid))
    log(db, 'recycling', e['id'], 'in_transit',
        f'{total}g left the depot; outbound note alone is not recycling', e['id'])
    return 'in_transit'


def plant_receive(db, e):
    """加工厂双边接收：门店与厂方共同确认后才进入加工环节。"""
    s = one(db, 'SELECT * FROM recycling_shipment WHERE id=?', (e['shipment_id'],))
    if not s or s['state'] != 'in_transit':
        raise ValueError('only an in-transit shipment can be bilaterally received')
    if not e.get('plant') or not e.get('at'):
        raise ValueError('bilateral reception needs plant and time')
    ids = [r['container_id'] for r in many(
        db, 'SELECT container_id FROM recycling_shipment_container WHERE shipment_id=? ORDER BY rowid',
        (s['id'],))]
    for cid in ids:
        c = _container(db, cid)
        _require_state(c, 'shipped')
        db.execute("UPDATE recycling_container SET state='received' WHERE id=?", (cid,))
    db.execute("UPDATE recycling_shipment SET state='received',plant=? WHERE id=?", (e['plant'], s['id']))
    log(db, 'recycling', s['id'], 'received',
        f'plant {e["plant"]} bilaterally received {len(ids)} container(s)', e['id'])
    return 'received'


# ---------------------------------------------------------------- 检测与事前比例
def contamination_check(db, e):
    if e['scope'] not in ('drop', 'batch'):
        raise ValueError('contamination scope must be drop or batch')
    if e['scope'] == 'drop':
        d = one(db, 'SELECT * FROM waste_drop WHERE id=?', (e['waste_drop_id'],))
        if not d:
            raise ValueError('drop-scope contamination needs the specific deposit')
        if _int(e, 'contaminant_grams') > d['mass_grams']:
            raise ValueError('contaminant cannot exceed the deposit gross mass')
        cid = None
    else:
        c = _container(db, e['container_id'])
        _require_state(c, 'received', 'processed')
        if _int(e, 'contaminant_grams') > _container_grams(db, c['id']):
            raise ValueError('batch contaminant cannot exceed container mass')
        cid = c['id']
    db.execute('INSERT INTO recycling_contamination(id,scope,container_id,waste_drop_id,contaminant_grams,'
               'at,inspector) VALUES(?,?,?,?,?,?,?)',
               (e['id'], e['scope'], cid, e.get('waste_drop_id'), _int(e, 'contaminant_grams'),
                e['at'], e['inspector']))
    log(db, 'recycling', e['id'], e['scope'],
        f'{e["contaminant_grams"]}g contaminant evidenced per {e["scope"]} test', e['id'])
    return e['scope']


def recyclable_ratio(db, e):
    """事前公布的可回收净重比例（基点，10000bp=100%），只能引用处置前公布的版本。"""
    if e['stream'] not in STREAMS or not (0 <= int(e['ratio_bp']) <= 10000):
        raise ValueError('stream and ratio within 0..10000 basis points required')
    db.execute('INSERT INTO recycling_ratio(id,stream,ratio_bp,published_at,note) VALUES(?,?,?,?,?)',
               (e['id'], e['stream'], int(e['ratio_bp']), e['published_at'], e.get('note', '')))
    log(db, 'recycling', e['id'], 'published',
        f'pre-published recyclable net ratio {e["ratio_bp"]}bp for {e["stream"]}', e['id'])
    return 'published'


# ---------------------------------------------------------------- 加工处置
def _drop_contaminants(db, household_id, stream, drop_id):
    r = one(db, "SELECT COALESCE(SUM(contaminant_grams),0) AS n FROM recycling_contamination "
                "WHERE scope='drop' AND waste_drop_id=?", (drop_id,))
    return r['n']


def _batch_contaminant(db, cid):
    r = one(db, "SELECT COALESCE(SUM(contaminant_grams),0) AS n FROM recycling_contamination "
                "WHERE scope='batch' AND container_id=?", (cid,))
    return r['n']


def _ratio(db, stream, before_at):
    r = one(db, 'SELECT * FROM recycling_ratio WHERE stream=? AND published_at<=? '
                'ORDER BY published_at DESC,id DESC LIMIT 1', (stream, before_at))
    if not r:
        raise ValueError('batch-only testing needs a pre-published recyclable net ratio')
    return r['ratio_bp']


def _allocate_sequence(steps, weights):
    """依次从同一质量池取量：每步在剩余权重上按整数比例分摊，保证逐切片不超取。

    所有显式步长之和必须恰等于池内总量（最后一步也不允许隐式吞掉余量），
    返回 {标签: [各切片克重]}，各切片各标签之和恰等于其原质量。
    """
    pool = list(weights)
    out = {label: [0] * len(weights) for _, label in steps}
    for idx, (amount, label) in enumerate(steps):
        total_pool = sum(pool)
        if amount > total_pool:
            raise ValueError(f'allocation step {label} exceeds remaining mass pool')
        if amount:
            parts = _allocate(amount, pool)
            for i, part in enumerate(parts):
                out[label][i] = part
                pool[i] -= part
        if idx == len(steps) - 1 and sum(pool) != 0:
            raise ValueError('allocation steps do not exhaust the mass pool')
    return out


def _post_reward_lot(db, household_id, cert_id, cid, grams, basis, eid):
    if grams <= 0:
        return
    points = grams // 100
    db.execute('INSERT INTO reward_lot(household_id,certificate_id,container_id,grams,points,state,basis,event_id) '
               "VALUES(?,?,?,?,?, 'credited',?,?)",
               (household_id, cert_id, cid, grams, points, basis, eid))


def processing_outcome(db, e):
    """加工厂处置证书：recycling（实际再生物+残渣）或 energy（合规能源回收，再生物为0）。"""
    c = _container(db, e['container_id'])
    _require_state(c, 'received')
    s = one(db, 'SELECT * FROM recycling_shipment_container JOIN recycling_shipment s '
                'ON s.id=shipment_id WHERE container_id=?', (c['id'],))
    if not s or s['state'] != 'received':
        raise ValueError('container has no bilateral plant reception; outbound note alone is not recycling')
    route = e['route']
    if route not in ROUTES:
        raise ValueError('processing route must be recycling or energy')
    if one(db, 'SELECT id FROM processing_certificate WHERE id=?', (e['certificate_id'],)):
        raise ValueError('certificate already used on another trip')
    incoming = _container_grams(db, c['id'])
    recycled = _int(e, 'recycled_grams')
    residue = _int(e, 'residue_grams')
    energy = int(e.get('energy_grams', 0))
    loss = int(e.get('loss_grams', 0))
    for v in (recycled, residue, energy, loss):
        if v < 0:
            raise ValueError('negative output mass forbidden')
    if route == 'recycling' and energy:
        raise ValueError('recycling route cannot claim recovered-energy mass')
    if route == 'energy' and recycled:
        raise ValueError('energy recovery yields zero actual recycled material; no recycling points allowed')
    slices = _slices(db, c['id'])
    batch_cont = _batch_contaminant(db, c['id'])
    drop_mode = any(_drop_contaminants(db, sl['household_id'], sl['stream'], sl['waste_drop_id'])
                    for sl in slices)
    batch_mode = batch_cont > 0
    if drop_mode and batch_mode:
        raise ValueError('per-deposit and batch contamination evidence cannot both govern one container')

    # 逐切片确定可归属的可回收净重（只用于积分归因，此刻不动台账）
    eligible = []
    contaminants = []
    for sl in slices:
        grams = sl['grams']
        if drop_mode:
            # 有逐户杂质检测：对应投递自己承担，绝不按毛重平均扣减到其他住户
            cont = _drop_contaminants(db, sl['household_id'], sl['stream'], sl['waste_drop_id'])
            if cont > grams:
                raise ValueError('per-deposit contaminant exceeds its attributable slice mass')
            contaminants.append(cont)
            grams -= cont
        else:
            contaminants.append(0)
            if batch_mode:
                # 只有混装后整体检测：按事前公布的可回收净重比例分摊（比例只用于积分归因，
                # 实体质量仍由证书的再生物/残渣守恒覆盖，不重复扣减）
                grams = grams * _ratio(db, sl['stream'], e['at']) // 10000
        eligible.append(max(grams, 0))
    eligible_total = sum(eligible)

    # 守恒以入厂实际容器质量为投入口径；逐户污染物已有独立检测依据并作为残渣类去向单列，
    # 证书产出（再生物+残渣+能源+损耗）与该去向之和必须精确等于投入，禁止负重量
    contaminant_out = sum(contaminants)
    if recycled + residue + energy + loss + contaminant_out != incoming:
        raise ValueError(
            f'mass conservation broken: {recycled}+{residue}+{energy}+{loss}+'
            f'{contaminant_out} != input {incoming}')

    db.execute('INSERT INTO processing_certificate(id,shipment_id,container_id,route,recycled_grams,'
               'residue_grams,energy_grams,loss_grams,at,plant,state,reason) '
               "VALUES(?,?,?,?,?,?,?,?,?,?,'valid',?)",
               (e['certificate_id'], s['shipment_id'], c['id'], route, recycled, residue, energy, loss,
                e['at'], e['plant'], e.get('reason', '')))

    if route == 'recycling':
        if recycled > eligible_total:
            raise ValueError(
                f'recycled output {recycled}g exceeds attributable recyclable net {eligible_total}g')
        # 先把有逐户检测依据的污染物作为残渣类去向从对应切片剔除（不进再生物、不摊给他人）
        for sl, cont in zip(slices, contaminants):
            if cont:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -cont,
                      'drop_contaminant_residue', sl['waste_drop_id'],
                      f'per-deposit contaminant removed per test {e["id"]}')
        # 再生物归因：逐户检测时每住户分到的再生物不得超过其自身可回收净重
        # （污染物由对应投递承担，绝不摊给他人）；整批比例/无检测时按净重比例分摊
        rec_parts = _allocate(recycled, eligible)
        if drop_mode and any(rec > elg for rec, elg in zip(rec_parts, eligible)):
            raise ValueError('per-deposit test: recycled attribution exceeds a household eligible net')
        rest_weights = [sl['grams'] - cont - rec
                        for sl, cont, rec in zip(slices, contaminants, rec_parts)]
        rest_alloc = _allocate_sequence([(residue, 'residue'), (loss, 'loss')], rest_weights)
        for sl, rec, res, los in zip(slices, rec_parts, rest_alloc['residue'], rest_alloc['loss']):
            if rec:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -rec,
                      'recycled_output', sl['waste_drop_id'], f'certificate {e["certificate_id"]}')
                _post_reward_lot(db, sl['household_id'], e['certificate_id'], c['id'], rec,
                                 'drop_test' if drop_mode else
                                 ('batch_ratio' if batch_mode else 'full_net'), e['id'])
            if res:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -res,
                      'residue', sl['waste_drop_id'], f'residue of certificate {e["certificate_id"]}')
            if los:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -los,
                      'process_loss', sl['waste_drop_id'], f'process loss of certificate {e["certificate_id"]}')
        log(db, 'recycling', e['certificate_id'], 'valid',
            f'recycling route: {recycled}g recycled, {residue}g residue, {loss}g loss', e['id'])
    else:
        # 能源回收：8 千克都有监管去向，但实际再生物为 0，两户不得领回收积分
        weights = [sl['grams'] for sl in slices]
        alloc = _allocate_sequence(
            [(energy, 'energy'), (residue, 'residue'), (loss, 'loss')], weights)
        for sl, en, res, los in zip(slices, alloc['energy'], alloc['residue'], alloc['loss']):
            if en:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -en,
                      'energy_recovery', sl['waste_drop_id'], f'energy certificate {e["certificate_id"]}')
            if res:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -res,
                      'residue', sl['waste_drop_id'], f'residue of certificate {e["certificate_id"]}')
            if los:
                _post(db, e['id'], c['id'], sl['household_id'], sl['stream'], -los,
                      'process_loss', sl['waste_drop_id'], f'process loss of certificate {e["certificate_id"]}')
        log(db, 'recycling', e['certificate_id'], 'valid',
            f'energy route: {energy}g to energy recovery, {residue}g residue, {loss}g loss; zero recycling points',
            e['id'])

    # 证书处置完成、容器腾空：物料全部有去向，容器可再投入使用
    db.execute("UPDATE recycling_container SET state='emptied' WHERE id=?", (c['id'],))
    households = {sl['household_id'] for sl in slices}
    for h in households:
        reward.decide(db, h, e['id'])
    return 'valid'


# ---------------------------------------------------------------- 证书重复与兑付
def certificate_reuse(db, e):
    """证书被另一车次重复引用：冻结该证书覆盖的积分，保留原入站与实体交接。"""
    cert = one(db, 'SELECT * FROM processing_certificate WHERE id=?', (e['certificate_id'],))
    if not cert:
        raise ValueError('only an issued certificate can be flagged for reuse')
    db.execute('UPDATE processing_certificate SET state=?,reuse_reference=?,reason=? WHERE id=?',
               ('duplicate', e.get('other_trip_id', ''),
                f'reused on another trip: {e.get("other_trip_id", "unknown")}', cert['id']))
    households = [r['household_id'] for r in many(
        db, 'SELECT DISTINCT household_id FROM reward_lot WHERE certificate_id=? ORDER BY household_id',
        (cert['id'],))]
    for h in households:
        db.execute("UPDATE reward_lot SET state='frozen' WHERE certificate_id=? AND household_id=?",
                   (cert['id'], h))
        reward.decide(db, h, e['id'])
    log(db, 'recycling', cert['id'], 'duplicate',
        f'certificate reuse on {e.get("other_trip_id", "unknown trip")}; covered points frozen', e['id'])
    return 'duplicate'


def reward_payout(db, e):
    """积分兑付：迟到复称/证书只能修订未兑付部分，已兑付部分形成可追偿差额。"""
    h = one(db, 'SELECT id FROM household WHERE id=?', (e['household_id'],))
    if not h:
        raise ValueError('unknown household')
    if _int(e, 'points') <= 0:
        raise ValueError('payout points must be positive')
    db.execute('INSERT INTO reward_payout(id,household_id,points,at) VALUES(?,?,?,?)',
               (e['id'], e['household_id'], _int(e, 'points'), e['at']))
    state = reward.decide(db, e['household_id'], e['id'])
    log(db, 'reward', e['household_id'], 'paid',
        f'{e["points"]} points cashed; later revisions only recover the difference', e['id'])
    return state
