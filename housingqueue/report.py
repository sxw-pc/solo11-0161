import hashlib
import json
from .store import connect,many,one
from .source import ingest
TABLES=['household','member','document','application_decision','income_check','property_check','objection','eligibility','queue_position','unit','offer','signature','allocation_dispute','waste_drop','waste_decision','reward_decision','decision_log',
'recycling_container','recycling_lot_ledger','recycling_shipment','recycling_shipment_container','recycling_contamination','recycling_ratio','processing_certificate','reward_lot','reward_payout','reward_recovery']
LEDGER_BUCKETS={'gross_pack':'packed_grams','tare_difference':'tare_difference_grams','evaporation_loss':'evaporation_loss_grams',
'drop_contaminant_residue':'drop_contaminant_grams','recycled_output':'recycled_output_grams','energy_recovery':'energy_recovery_grams','residue':'residue_grams','process_loss':'process_loss_grams'}
def report(db):
    r={t:[dict(x) for x in many(db,f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}
    r['event']=[dict(x) for x in many(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    r['source_file']=[dict(x) for x in many(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    r['recycling_mass_flow']=mass_flow(db)
    r['household_verified_points']=household_points(db)
    return r
def mass_flow(db):
    """按物理去向分桶列质量；在途不等于已回收，再生物必须有加工厂证书。"""
    inbound=one(db,'SELECT COALESCE(SUM(mass_grams),0) AS n FROM waste_drop')['n']
    buckets=dict.fromkeys(LEDGER_BUCKETS.values(),0)
    for row in many(db,'SELECT basis,COALESCE(SUM(ABS(change_grams)),0) AS n FROM recycling_lot_ledger GROUP BY basis'):
        if row['basis'] in LEDGER_BUCKETS:buckets[LEDGER_BUCKETS[row['basis']]]=row['n']
    state_mass={s:0 for s in ('sealed','tared','shipped','received','processed','emptied','merged')}
    containers=[]
    # 容器级守恒：正量（装箱/拆入/并入）与负量（拆走/合并转出/各项处置去向）必须逐容器平衡，
    # 容器总余额不得为负；同一投入在拆合后只以转移形式出现，全局口径再核对一次去向总和
    conservation_ok=True
    for c in many(db,'SELECT * FROM recycling_container ORDER BY rowid'):
        grams=one(db,'SELECT COALESCE(SUM(change_grams),0) AS n FROM recycling_lot_ledger WHERE container_id=?',(c['id'],))['n']
        if grams<0:conservation_ok=False
        packed=one(db,"SELECT COALESCE(SUM(change_grams),0) AS n FROM recycling_lot_ledger WHERE container_id=? AND change_grams>0",(c['id'],))['n']
        removed=one(db,"SELECT COALESCE(SUM(-change_grams),0) AS n FROM recycling_lot_ledger WHERE container_id=? AND change_grams<0",(c['id'],))['n']
        if packed!=grams+removed:conservation_ok=False
        # 逐户污染物是有检测依据的残渣类离开量，加工后允许容器归零，但不允许出现无依据的负切片
        open_slices=many(db,"SELECT household_id,stream,waste_drop_id,SUM(change_grams) g FROM recycling_lot_ledger WHERE container_id=? GROUP BY household_id,stream,waste_drop_id HAVING g<0",(c['id'],))
        if open_slices and c['state'] not in ('emptied','merged'):conservation_ok=False
        state_mass[c['state']]=state_mass.get(c['state'],0)+grams
        containers.append({'id':c['id'],'state':c['state'],'tare_grams':c['tare_grams'],'gross_grams':c['gross_grams'],
                           'net_grams':c['net_grams'],'current_grams':grams,'packed_grams':packed,'removed_grams':removed})
    # 全局守恒：原始装箱量 == 仍在容器中的质量 + 全部有据可查的离开量
    # （拆合只在容器间转移：同一事件的正负记录在 held 中跨容器对冲，不产生也不消灭质量）
    leaves=sum(buckets[k] for k in ('tare_difference_grams','evaporation_loss_grams','drop_contaminant_grams',
        'recycled_output_grams','energy_recovery_grams','residue_grams','process_loss_grams'))
    held=sum(state_mass.values())
    if buckets['packed_grams']!=held+leaves:conservation_ok=False
    cert=one(db,"SELECT COALESCE(SUM(recycled_grams),0) AS r,COALESCE(SUM(residue_grams),0) AS s,COALESCE(SUM(energy_grams),0) AS e,COALESCE(SUM(loss_grams),0) AS l FROM processing_certificate WHERE state='valid'")
    frozen=one(db,"SELECT COALESCE(SUM(c.recycled_grams),0) AS n FROM processing_certificate c WHERE c.state='duplicate'")['n']
    # 出站但加工厂尚未双边接收：仅凭门店出站单，只能算在途
    unconfirmed=one(db,"SELECT COALESCE(SUM(l.grams),0) AS n FROM (SELECT container_id,SUM(change_grams) grams FROM recycling_lot_ledger GROUP BY container_id) l JOIN recycling_container c ON c.id=l.container_id WHERE c.state IN ('shipped')")['n']
    return {**buckets,
      'inbound_gross_grams':inbound,
      'in_transit_grams':state_mass.get('shipped',0),
      'at_plant_pending_processing_grams':state_mass.get('received',0),
      'depot_pending_shipment_grams':state_mass.get('sealed',0)+state_mass.get('tared',0),
      'recycled_certified_grams':cert['r'],'recycled_frozen_certificate_grams':frozen,
      'energy_recovery_certified_grams':cert['e'],'residue_certified_grams':cert['s'],
      'process_loss_certified_grams':cert['l'],
      'pending_verification_grams':state_mass.get('sealed',0)+state_mass.get('tared',0)+state_mass.get('shipped',0)+state_mass.get('received',0),
      'outbound_without_plant_reception_grams':unconfirmed,
      'conservation_ok':conservation_ok,'containers':containers}
def household_points(db):
    """每户已核积分只认可归属实际再生物；冻结、已兑付与可追偿差额分列。"""
    ids=[r['household_id'] for r in many(db,'SELECT DISTINCT household_id FROM waste_drop UNION SELECT DISTINCT household_id FROM reward_lot ORDER BY household_id')]
    out=[]
    for hid in ids:
        gross=one(db,'SELECT COALESCE(SUM(mass_grams),0) AS n FROM waste_drop WHERE household_id=?',(hid,))['n']
        cred=one(db,"SELECT COALESCE(SUM(grams),0) g,COALESCE(SUM(points),0) p FROM reward_lot WHERE household_id=? AND state='credited'",(hid,))
        fro=one(db,"SELECT COALESCE(SUM(grams),0) g,COALESCE(SUM(points),0) p FROM reward_lot WHERE household_id=? AND state='frozen'",(hid,))
        paid=one(db,'SELECT COALESCE(SUM(points),0) AS n FROM reward_payout WHERE household_id=?',(hid,))['n']
        recover=one(db,'SELECT COALESCE(SUM(points),0) AS n FROM reward_recovery WHERE household_id=?',(hid,))['n']
        decision=one(db,'SELECT * FROM reward_decision WHERE household_id=?',(hid,))
        out.append({'household_id':hid,'inbound_gross_grams':gross,
          'verified_recycled_grams':cred['g'],'verified_points':cred['p'],
          'frozen_certificate_grams':fro['g'],'frozen_points':fro['p'],
          'paid_points':paid,'recoverable_points':recover,
          'decision_state':decision['state'] if decision else 'pending'})
    return out
def audit(db,handlers):
    replay=connect(':memory:')
    for src in many(db,'SELECT command,sha,raw FROM source_file ORDER BY id'):
        if hashlib.sha256(src['raw']).hexdigest()!=src['sha']:return {'replay_ok':False,'mismatches':['source_file_sha']}
        with replay:
            for e in json.loads(src['raw'])['events']:
                if e['kind'] not in handlers[src['command']]:raise ValueError('recorded command/kind mismatch')
                if ingest(replay,e):handlers[src['command']][e['kind']](replay,e)
            replay.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(src['command'],src['sha'],src['raw']))
    old,new=report(db),report(replay)
    for r in (old,new):
        for x in r['decision_log']:x.pop('at',None)
    replay.close()
    return {'replay_ok':old==new,'mismatches':[k for k in old if old[k]!=new[k]],'event_count':len(old['event'])}
