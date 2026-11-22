import json
import tempfile
import unittest
from pathlib import Path

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import report, audit
from housingqueue.source import ingest
from housingqueue.store import connect

ROOT = Path(__file__).resolve().parents[1]
BASE = ['recycling-application.json', 'recycling-eligibility.json',
        'recycling-allocation.json', 'recycling-waste.json']


class RecyclingCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'test.sqlite')
        for name in BASE:
            import_file(self.db, 'application-import' if name.endswith('application.json')
                        else 'eligibility-import' if name.endswith('eligibility.json')
                        else 'allocation-import' if name.endswith('allocation.json')
                        else 'waste-import', ROOT / 'fixtures' / name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def load(self, name):
        return import_file(self.db, 'recycling-import', ROOT / 'fixtures' / name)

    def events(self, events):
        p = Path(self.temp.name) / 'events.json'
        p.write_text(json.dumps({'events': events}))
        return import_file(self.db, 'recycling-import', p)

    def containers(self):
        return {c['id']: c for c in report(self.db)['recycling_mass_flow']['containers']}

    # ------------------------------------------------ 路线一：分拣再生物
    def test_recycling_route_points_follow_attributable_recyclate_not_gross(self):
        self.load('recycling-route.json')
        flow = report(self.db)['recycling_mass_flow']
        self.assertTrue(flow['conservation_ok'])
        # 出站 8 千克不等于回收 8 千克：实际再生物只有 6 千克
        self.assertEqual(flow['recycled_certified_grams'], 6000)
        self.assertEqual(flow['residue_certified_grams'], 2000)
        self.assertEqual(flow['pending_verification_grams'], 0)
        hp = {h['household_id']: h for h in report(self.db)['household_verified_points']}
        # 按事前公布的 75% 可回收净重比例分摊（整批检测），不按毛重平均扣减
        self.assertEqual(hp['family-a']['verified_recycled_grams'], 3000)
        self.assertEqual(hp['family-b']['verified_recycled_grams'], 3000)
        self.assertEqual(hp['family-a']['verified_points'], 30)
        self.assertEqual(hp['family-b']['verified_points'], 30)
        # 入站毛重没有被改写
        self.assertEqual(hp['family-a']['inbound_gross_grams'], 5000)
        c = self.containers()['SEAL-C1']
        self.assertEqual(c['state'], 'emptied')
        self.assertEqual(c['current_grams'], 0)
        self.assertEqual(c['net_grams'], 8000)

    def test_certificate_reuse_freezes_points_but_keeps_physical_handover(self):
        self.load('recycling-route.json')
        self.load('recycling-reuse.json')
        hp = {h['household_id']: h for h in report(self.db)['household_verified_points']}
        self.assertEqual(hp['family-a']['frozen_points'], 30)
        self.assertEqual(hp['family-b']['frozen_points'], 30)
        # 冻结后未兑付的已核积分归零，但不伪造负重量
        self.assertEqual(hp['family-a']['verified_points'], 0)
        self.assertEqual(hp['family-a']['verified_recycled_grams'], 0)
        # 已兑付 30 分形成可追偿差额
        self.assertEqual(hp['family-a']['paid_points'], 30)
        self.assertEqual(hp['family-a']['recoverable_points'], 30)
        self.assertEqual(hp['family-a']['decision_state'], 'recoverable')
        # 原入站称重与实体交接保留
        self.assertEqual(hp['family-a']['inbound_gross_grams'], 5000)
        cert = self.db.execute(
            "SELECT state,reuse_reference FROM processing_certificate WHERE id='CERT-RC1'").fetchone()
        self.assertEqual(cert[0], 'duplicate')
        self.assertEqual(cert[1], 'SHIP-FRAUD-9')
        drops = self.db.execute('SELECT COUNT(*) FROM waste_drop').fetchone()[0]
        self.assertEqual(drops, 3)
        flow = report(self.db)['recycling_mass_flow']
        self.assertEqual(flow['recycled_frozen_certificate_grams'], 6000)
        self.assertTrue(flow['conservation_ok'])

    # ------------------------------------------------ 路线二：合规能源回收
    def test_energy_route_disposes_everything_but_awards_no_recycling_points(self):
        self.load('recycling-energy.json')
        flow = report(self.db)['recycling_mass_flow']
        self.assertTrue(flow['conservation_ok'])
        self.assertEqual(flow['energy_recovery_certified_grams'], 7300)
        self.assertEqual(flow['residue_certified_grams'], 500)
        self.assertEqual(flow['process_loss_certified_grams'], 200)
        self.assertEqual(flow['recycled_certified_grams'], 0)
        for h in report(self.db)['household_verified_points']:
            self.assertEqual(h['verified_points'], 0)
            self.assertEqual(h['verified_recycled_grams'], 0)
            self.assertEqual(h['decision_state'], 'pending')
        # 没有任何 reward_lot，积分账上不出现“回收成果”
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reward_lot').fetchone()[0], 0)

    # ------------------------------------------------ 容器拆合
    def test_container_split_and_merge_never_double_count_mass(self):
        self.load('recycling-split-merge.json')
        c = self.containers()
        # S2 分出 600 后剩 900；T1 的 600 并入 S1 后成为 merged 空容器
        self.assertEqual(c['SEAL-S2']['current_grams'], 900)
        self.assertEqual(c['SEAL-T1']['state'], 'merged')
        self.assertEqual(c['SEAL-T1']['current_grams'], 0)
        # S1 = 金属 1000 + 并入的纸 600 = 1600，去皮净重一致
        self.assertEqual(c['SEAL-S1']['net_grams'], 1600)
        self.assertEqual(c['SEAL-S1']['current_grams'], 0)
        flow = report(self.db)['recycling_mass_flow']
        self.assertTrue(flow['conservation_ok'])
        # 同一笔投递的纸在 S2 与 S1 两处的净持有量合计仍只有 1500，没有重复出现
        def held(container_id):
            return self.db.execute(
                'SELECT COALESCE(SUM(change_grams),0) FROM recycling_lot_ledger '
                "WHERE container_id=? AND waste_drop_id='WASTE-H1'", (container_id,)).fetchone()[0]
        # S1 处置后已腾空，查它曾持有的 WASTE-H1 正量；S2 仍留 900
        s1_held = self.db.execute(
            "SELECT COALESCE(SUM(change_grams),0) FROM recycling_lot_ledger "
            "WHERE container_id='SEAL-S1' AND waste_drop_id='WASTE-H1' AND change_grams>0").fetchone()[0]
        self.assertEqual(held('SEAL-S2'), 900)
        self.assertEqual(s1_held, 600)
        self.assertEqual(held('SEAL-S2') + s1_held, 1500)
        # 再生物 1550 全归 family-a（唯一住户），残渣 50；1550//100=15 分
        hp = report(self.db)['household_verified_points'][0]
        self.assertEqual(hp['verified_recycled_grams'], 1550)
        self.assertEqual(hp['verified_points'], 14)
        self.assertEqual(hp['household_id'], 'family-a')

    # ------------------------------------------------ 去皮差异与蒸发
    def test_tare_difference_and_evaporation_are_separately_evidenced(self):
        self.load('recycling-tare.json')
        flow = report(self.db)['recycling_mass_flow']
        self.assertTrue(flow['conservation_ok'])
        # 装箱毛重 5000，去皮差异 300（毛重 5150 - 皮重 450 = 净重 4700）
        self.assertEqual(flow['packed_grams'], 5000)
        self.assertEqual(flow['tare_difference_grams'], 300)
        self.assertEqual(flow['evaporation_loss_grams'], 200)
        c = self.containers()['SEAL-D1']
        self.assertEqual(c['net_grams'], 4700)
        self.assertEqual(c['current_grams'], 4500)
        self.assertEqual(c['state'], 'tared')
        # 去皮差异按实际切片分摊，不产生负重量
        self.assertGreaterEqual(min(r['grams'] for r in self.db.execute(
            "SELECT household_id,SUM(change_grams) grams FROM recycling_lot_ledger "
            "WHERE container_id='SEAL-D1' GROUP BY household_id").fetchall()), 0)
        # 容器仍在门店，属于待核，不能算已回收
        self.assertEqual(flow['pending_verification_grams'], 4500)
        self.assertEqual(flow['recycled_certified_grams'], 0)

    # ------------------------------------------------ 质量守恒拒绝
    def test_certificate_violating_conservation_is_rejected_and_atomic(self):
        before = self.db.execute('SELECT COUNT(*) FROM processing_certificate').fetchone()[0]
        with self.assertRaises(ValueError):
            self.load('recycling-bad-conservation.json')
        # 整文件事务回滚：没有留下半张证书或半条台账
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM processing_certificate').fetchone()[0], before)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM recycling_container WHERE id='SEAL-B1'").fetchone()[0], 0)

    # ------------------------------------------------ 在途不等于回收
    def test_outbound_without_plant_reception_stays_in_transit(self):
        self.events([
            {'id': 'SEAL-X1', 'kind': 'container_seal', 'sealed_at': '2026-04-03T10:00:00', 'tare_grams': 100},
            {'id': 'PACK-X1', 'kind': 'drop_pack', 'container_id': 'SEAL-X1',
             'waste_drop_id': 'WASTE-H1-M', 'household_id': 'family-a', 'stream': 'metal', 'packed_grams': 1000},
            {'id': 'TARE-X1', 'kind': 'container_tare', 'container_id': 'SEAL-X1',
             'gross_grams': 1100, 'weigher': 'depot-scale-a', 'at': '2026-04-03T10:30:00'},
            {'id': 'SHIP-X1', 'kind': 'shipment_out', 'container_ids': ['SEAL-X1'],
             'at': '2026-04-03T11:00:00'}])
        flow = report(self.db)['recycling_mass_flow']
        self.assertEqual(flow['in_transit_grams'], 1000)
        self.assertEqual(flow['outbound_without_plant_reception_grams'], 1000)
        self.assertEqual(flow['recycled_certified_grams'], 0)
        with self.assertRaises(ValueError):
            self.events([{'id': 'CERT-X', 'kind': 'processing_outcome', 'certificate_id': 'CERT-X',
                          'container_id': 'SEAL-X1', 'route': 'recycling',
                          'recycled_grams': 1000, 'residue_grams': 0,
                          'at': '2026-04-03T15:00:00', 'plant': 'ghost-plant'}])

    # ------------------------------------------------ 逐户杂质检测
    def test_per_deposit_contamination_is_borne_by_the_responsible_deposit(self):
        self.events([
            {'id': 'SEAL-Q1', 'kind': 'container_seal', 'sealed_at': '2026-04-03T10:00:00', 'tare_grams': 100},
            {'id': 'PACK-Q1', 'kind': 'drop_pack', 'container_id': 'SEAL-Q1',
             'waste_drop_id': 'WASTE-H1', 'household_id': 'family-a', 'stream': 'paper', 'packed_grams': 4000},
            {'id': 'PACK-Q2', 'kind': 'drop_pack', 'container_id': 'SEAL-Q1',
             'waste_drop_id': 'WASTE-H2', 'household_id': 'family-b', 'stream': 'plastic', 'packed_grams': 4000},
            {'id': 'TARE-Q1', 'kind': 'container_tare', 'container_id': 'SEAL-Q1',
             'gross_grams': 8100, 'weigher': 'depot-scale-a', 'at': '2026-04-03T10:30:00'},
            {'id': 'SHIP-Q1', 'kind': 'shipment_out', 'container_ids': ['SEAL-Q1'], 'at': '2026-04-03T11:00:00'},
            {'id': 'RECV-Q1', 'kind': 'plant_receive', 'shipment_id': 'SHIP-Q1',
             'plant': 'recycling-plant-1', 'at': '2026-04-03T13:00:00'},
            {'id': 'CONT-DROP', 'kind': 'contamination_check', 'scope': 'drop',
             'waste_drop_id': 'WASTE-H1', 'contaminant_grams': 1000,
             'at': '2026-04-04T09:00:00', 'inspector': 'plant-lab'},
            {'id': 'CERT-Q1', 'kind': 'processing_outcome', 'certificate_id': 'CERT-Q1',
             'container_id': 'SEAL-Q1', 'route': 'recycling',
             'recycled_grams': 7000, 'residue_grams': 0,
             'at': '2026-04-04T15:00:00', 'plant': 'recycling-plant-1'}])
        flow = report(self.db)['recycling_mass_flow']
        self.assertTrue(flow['conservation_ok'])
        # 污染物由对应投递独立承担并实体剔除（1000g 残渣类独立依据；证书本身无残渣）
        self.assertEqual(flow['drop_contaminant_grams'], 1000)
        self.assertEqual(flow['residue_certified_grams'], 0)
        hp = {h['household_id']: h for h in report(self.db)['household_verified_points']}
        # family-a 的投递承担 1000g 杂质：可归属净重 3000；family-b 无杂质，全额 4000
        self.assertEqual(hp['family-a']['verified_recycled_grams'], 3000)
        self.assertEqual(hp['family-b']['verified_recycled_grams'], 4000)
        self.assertEqual(hp['family-a']['verified_points'], 30)
        self.assertEqual(hp['family-b']['verified_points'], 40)
        lot_basis = self.db.execute(
            "SELECT DISTINCT basis FROM reward_lot WHERE certificate_id='CERT-Q1'").fetchall()
        self.assertEqual([r[0] for r in lot_basis], ['drop_test'])

    # ------------------------------------------------ 幂等与异内容冲突
    def test_same_content_replay_idempotent_different_content_rejected(self):
        self.load('recycling-route.json')
        again = self.load('recycling-route.json')
        self.assertEqual(again, [{'result': 'file_replayed'}])
        # 逐事件重放（同文件内容的事件）也保持幂等
        single = json.loads((ROOT / 'fixtures' / 'recycling-route.json').read_text())['events'][:1]
        p = Path(self.temp.name) / 'one.json'
        p.write_text(json.dumps({'events': single}))
        self.assertEqual(import_file(self.db, 'recycling-import', p)[0]['result'], 'event_replayed')
        with self.assertRaises(ValueError):
            self.events([{'id': 'SEAL-C1', 'kind': 'container_seal',
                          'sealed_at': '2026-04-03T10:00:00', 'tare_grams': 999}])
        # 原封签皮重未被改写
        self.assertEqual(self.containers()['SEAL-C1']['tare_grams'], 500)

    def test_replay_audit_rebuilds_mass_flow_and_points(self):
        self.load('recycling-route.json')
        self.load('recycling-reuse.json')
        result = audit(self.db, HANDLERS)
        self.assertTrue(result['replay_ok'])
        self.assertEqual(result['mismatches'], [])

    # ------------------------------------------------ 迟到复称的积分修订
    def test_late_tare_revision_only_adjusts_unpaid_points(self):
        self.events([
            {'id': 'SEAL-V1', 'kind': 'container_seal', 'sealed_at': '2026-04-03T10:00:00', 'tare_grams': 100},
            {'id': 'PACK-V1', 'kind': 'drop_pack', 'container_id': 'SEAL-V1',
             'waste_drop_id': 'WASTE-H1-M', 'household_id': 'family-a', 'stream': 'metal', 'packed_grams': 1000},
            {'id': 'TARE-V1', 'kind': 'container_tare', 'container_id': 'SEAL-V1',
             'gross_grams': 1100, 'weigher': 'depot-scale-a', 'at': '2026-04-03T10:30:00'},
            {'id': 'SHIP-V1', 'kind': 'shipment_out', 'container_ids': ['SEAL-V1'], 'at': '2026-04-03T11:00:00'},
            {'id': 'RECV-V1', 'kind': 'plant_receive', 'shipment_id': 'SHIP-V1',
             'plant': 'recycling-plant-1', 'at': '2026-04-03T13:00:00'},
            {'id': 'CERT-V1', 'kind': 'processing_outcome', 'certificate_id': 'CERT-V1',
             'container_id': 'SEAL-V1', 'route': 'recycling',
             'recycled_grams': 1000, 'residue_grams': 0,
             'at': '2026-04-04T15:00:00', 'plant': 'recycling-plant-1'},
            {'id': 'PAY-V1', 'kind': 'reward_payout', 'household_id': 'family-a',
             'points': 10, 'at': '2026-04-05T09:00:00'}])
        hp = {h['household_id']: h for h in report(self.db)['household_verified_points']}
        self.assertEqual(hp['family-a']['paid_points'], 10)
        # 次日证书被举报重复：冻结覆盖积分，已兑付的 10 分转为可追偿，不出现负数
        self.events([{'id': 'REUSE-V1', 'kind': 'certificate_reuse',
                      'certificate_id': 'CERT-V1', 'other_trip_id': 'SHIP-OTHER',
                      'at': '2026-04-06T10:00:00'}])
        hp = {h['household_id']: h for h in report(self.db)['household_verified_points']}
        self.assertEqual(hp['family-a']['verified_points'], 0)
        self.assertEqual(hp['family-a']['recoverable_points'], 10)
        self.assertEqual(hp['family-a']['decision_state'], 'recoverable')

    # ------------------------------------------------ 守恒与状态机的负路径
    def _chain_to_plant(self, seal_id='SEAL-N1'):
        return [
            {'id': seal_id, 'kind': 'container_seal', 'sealed_at': '2026-04-03T10:00:00', 'tare_grams': 100},
            {'id': 'PACK-' + seal_id, 'kind': 'drop_pack', 'container_id': seal_id,
             'waste_drop_id': 'WASTE-H1-M', 'household_id': 'family-a', 'stream': 'metal', 'packed_grams': 1000},
            {'id': 'TARE-' + seal_id, 'kind': 'container_tare', 'container_id': seal_id,
             'gross_grams': 1100, 'weigher': 'depot-scale-a', 'at': '2026-04-03T10:30:00'},
            {'id': 'SHIP-' + seal_id, 'kind': 'shipment_out', 'container_ids': [seal_id],
             'at': '2026-04-03T11:00:00'},
            {'id': 'RECV-' + seal_id, 'kind': 'plant_receive', 'shipment_id': 'SHIP-' + seal_id,
             'plant': 'recycling-plant-1', 'at': '2026-04-03T13:00:00'}]

    def test_same_deposit_cannot_be_packed_beyond_its_gross(self):
        # WASTE-H1-M 只有 1000g：先装 600，再装 500 就超过毛重，必须拒绝而不是重复计入
        self.events([
            {'id': 'SEAL-N2', 'kind': 'container_seal', 'sealed_at': '2026-04-03T10:00:00', 'tare_grams': 50},
            {'id': 'PACK-N2', 'kind': 'drop_pack', 'container_id': 'SEAL-N2',
             'waste_drop_id': 'WASTE-H1-M', 'household_id': 'family-a', 'stream': 'metal', 'packed_grams': 600},
            {'id': 'SEAL-N3', 'kind': 'container_seal', 'sealed_at': '2026-04-03T10:01:00', 'tare_grams': 50}])
        with self.assertRaises(ValueError):
            self.events([
                {'id': 'PACK-N3', 'kind': 'drop_pack', 'container_id': 'SEAL-N3',
                 'waste_drop_id': 'WASTE-H1-M', 'household_id': 'family-a', 'stream': 'metal',
                 'packed_grams': 500}])
        # 已装入的 600g 仍然有效，未产生重复或负量
        self.assertEqual(self.db.execute(
            "SELECT COALESCE(SUM(change_grams),0) FROM recycling_lot_ledger "
            "WHERE waste_drop_id='WASTE-H1-M' AND change_grams>0").fetchone()[0], 600)

    def test_energy_route_rejects_any_recycled_mass(self):
        chain = self._chain_to_plant()
        chain.append({'id': 'CERT-N1', 'kind': 'processing_outcome', 'certificate_id': 'CERT-N1',
                      'container_id': 'SEAL-N1', 'route': 'energy',
                      'recycled_grams': 100, 'residue_grams': 0, 'energy_grams': 900,
                      'at': '2026-04-03T18:00:00', 'plant': 'energy-plant-1'})
        with self.assertRaises(ValueError):
            self.events(chain)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reward_lot').fetchone()[0], 0)

    def test_certificate_id_cannot_serve_two_trips(self):
        chain = self._chain_to_plant()
        chain.append({'id': 'CERT-N2', 'kind': 'processing_outcome', 'certificate_id': 'CERT-N2',
                      'container_id': 'SEAL-N1', 'route': 'recycling',
                      'recycled_grams': 1000, 'residue_grams': 0,
                      'at': '2026-04-04T15:00:00', 'plant': 'recycling-plant-1'})
        self.events(chain)
        chain2 = self._chain_to_plant('SEAL-N4')
        chain2.append({'id': 'CERT-N2B', 'kind': 'processing_outcome', 'certificate_id': 'CERT-N2',
                       'container_id': 'SEAL-N4', 'route': 'recycling',
                       'recycled_grams': 1000, 'residue_grams': 0,
                       'at': '2026-04-04T16:00:00', 'plant': 'recycling-plant-1'})
        with self.assertRaises(ValueError):
            self.events(chain2)

    def test_batch_contamination_without_prepublished_ratio_is_rejected(self):
        chain = self._chain_to_plant('SEAL-N5')
        chain += [
            {'id': 'CONT-N5', 'kind': 'contamination_check', 'scope': 'batch',
             'container_id': 'SEAL-N5', 'contaminant_grams': 200,
             'at': '2026-04-04T09:00:00', 'inspector': 'plant-lab'},
            {'id': 'CERT-N5', 'kind': 'processing_outcome', 'certificate_id': 'CERT-N5',
             'container_id': 'SEAL-N5', 'route': 'recycling',
             'recycled_grams': 800, 'residue_grams': 200,
             'at': '2026-04-04T15:00:00', 'plant': 'recycling-plant-1'}]
        with self.assertRaises(ValueError):
            self.events(chain)

    def test_negative_output_mass_is_refused(self):
        chain = self._chain_to_plant('SEAL-N6')
        chain.append({'id': 'CERT-N6', 'kind': 'processing_outcome', 'certificate_id': 'CERT-N6',
                      'container_id': 'SEAL-N6', 'route': 'recycling',
                      'recycled_grams': 1100, 'residue_grams': -100,
                      'at': '2026-04-04T15:00:00', 'plant': 'recycling-plant-1'})
        with self.assertRaises(ValueError):
            self.events(chain)


if __name__ == '__main__':
    unittest.main()
