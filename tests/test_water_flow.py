import json
import tempfile
import unittest
from pathlib import Path

from housingqueue.store import connect, one, many
from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import report, audit
from housingqueue.water import occupant
from housingqueue import water_flow

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
BASE = [('application-import', 'application-ok.json'),
        ('eligibility-import', 'eligibility-ok.json'),
        ('allocation-import', 'allocation-ok.json'),
        ('allocation-import', 'unit-u2.json'),
        ('water-import', 'water-flow-taps.json')]


class WaterFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'flow.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def load_base(self):
        for command, name in BASE:
            import_file(self.db, command, FIX / name)

    def load(self, name):
        self.load_base()
        return import_file(self.db, 'water-flow-import', FIX / name)

    def directives(self, building):
        return many(self.db,
                    'SELECT * FROM flow_directive WHERE building=? ORDER BY minute,id', (building,))

    def minutes(self, building, node):
        return many(self.db,
                    'SELECT * FROM flow_minute WHERE building=? AND node_id=? ORDER BY minute',
                    (building, node))

    def test_occupant_follows_signed_allocation_and_vacant_unit_has_no_household(self):
        self.load_base()
        self.assertEqual(occupant(self.db, 'U1'), 'family-a')
        self.assertIsNone(occupant(self.db, 'U2'))
        # A later eligibility dispute preserves the occupancy fact.
        import_file(self.db, 'eligibility-import', FIX / 'eligibility-bad.json')
        self.assertEqual(
            one(self.db, "SELECT status FROM household WHERE id='family-a'")['status'],
            'housed_disputed')
        self.assertEqual(occupant(self.db, 'U1'), 'family-a')

    def test_tank_mixes_after_outflow_and_never_copies_upstream_value_to_tap(self):
        self.load('water-flow-route-beta.json')
        tank = {r['minute']: r for r in self.minutes('tower-b', 'tank-b')}
        self.assertEqual(tank[0]['cfu'], 160.0)
        self.assertEqual(tank[3]['cfu'], 81.92)
        tap = {r['minute']: r for r in self.minutes('tower-b', 'FTAP-U1')}
        # At minute 3 the tank is below threshold but the tap still delivers 160.
        self.assertEqual(tap[3]['cfu'], 160.0)
        self.assertEqual(tap[6]['cfu'], 81.92)
        self.assertEqual(tap[7]['cfu'], 65.536)

    def test_pipe_retention_keeps_parcels_when_no_water_arrives(self):
        self.load('water-flow-route-alpha.json')
        pipe = {r['minute']: r for r in self.minutes('tower-a', 'pipe-a-u1')}
        for m in range(1, 5):
            self.assertEqual(pipe[m]['volume'], 40.0)
            self.assertEqual(pipe[m]['cfu'], 140.0)
            self.assertEqual(pipe[m]['inflow'], 0.0)
            self.assertEqual(pipe[m]['outflow'], 0.0)
        # The retained parcel is still the original seed water after the valve reopens.
        seed = one(self.db,
                   "SELECT * FROM flow_parcel WHERE building='tower-a' AND location='pipe-a-u1' "
                   "AND parcel_uid='pipe-a-u1-seed' ORDER BY minute DESC")
        self.assertIsNotNone(seed)
        self.assertEqual(seed['cfu'], 140.0)

    def test_route_alpha_interruption_bottled_then_resample_restores_direct(self):
        self.load('water-flow-route-alpha.json')
        rows = self.directives('tower-a')
        self.assertEqual(rows[0]['directive'], 'boil')
        for m in range(1, 5):
            self.assertEqual(rows[m]['directive'], 'bottled')
            self.assertEqual(rows[m]['supply'], 'interrupted')
            self.assertIn('valve valve-a-in closed', rows[m]['basis'])
            self.assertEqual(rows[m]['notify_target'], 'family-a')
        self.assertEqual(rows[5]['directive'], 'boil')
        self.assertIn('S-A3-QCFAIL', rows[5]['basis'])
        # Simulated clearance at minute 9 is not enough on its own.
        self.assertEqual(rows[9]['directive'], 'boil')
        self.assertEqual(rows[10]['directive'], 'direct')
        self.assertIn('resample S-A10', rows[10]['basis'])

    def test_route_beta_boil_through_normal_use_and_no_clearance_at_minute_four(self):
        self.load('water-flow-route-beta.json')
        rows = self.directives('tower-b')
        for m in range(0, 7):
            self.assertEqual(rows[m]['directive'], 'boil', msg='minute %d' % m)
            self.assertEqual(rows[m]['supply'], 'delivered')
        tank4 = one(self.db,
                    "SELECT cfu FROM flow_minute WHERE building='tower-b' AND node_id='tank-b' AND minute=4")
        self.assertLess(tank4['cfu'], 100.0)
        # Tank clearance must not be copied onto the tap order.
        self.assertEqual(rows[4]['directive'], 'boil')
        self.assertEqual(rows[7]['directive'], 'direct')
        self.assertIn('resample S-B6', rows[7]['basis'])

    def test_routes_disagree_at_minute_four_but_agree_tank_is_not_enough(self):
        alpha = connect(':memory:')
        beta = connect(':memory:')
        try:
            for command, name in BASE:
                import_file(alpha, command, FIX / name)
                import_file(beta, command, FIX / name)
            import_file(alpha, 'water-flow-import', FIX / 'water-flow-route-alpha.json')
            import_file(beta, 'water-flow-import', FIX / 'water-flow-route-beta.json')
            a = {r['minute']: r for r in many(
                alpha, "SELECT * FROM flow_directive WHERE building='tower-a'")}
            b = {r['minute']: r for r in many(
                beta, "SELECT * FROM flow_directive WHERE building='tower-b'")}
            self.assertEqual((a[4]['directive'], a[4]['supply']), ('bottled', 'interrupted'))
            self.assertEqual((b[4]['directive'], b[4]['supply']), ('boil', 'delivered'))
            self.assertNotEqual(a[4]['directive'], 'direct')
            self.assertNotEqual(b[4]['directive'], 'direct')
        finally:
            alpha.close()
            beta.close()

    def test_pump_outage_is_interruption_and_unapproved_action_does_not_change_flow(self):
        self.load('water-flow-pump-outage.json')
        pump = {r['minute']: r for r in self.minutes('tower-c', 'pump-c')}
        self.assertEqual(pump[1]['pump_on'], 1)
        self.assertEqual(pump[2]['pump_on'], 0)
        rows = self.directives('tower-c')
        self.assertEqual(rows[1]['directive'], 'boil')
        self.assertEqual(rows[2]['directive'], 'bottled')
        self.assertIn('pump pump-c stopped', rows[2]['basis'])
        self.assertEqual(rows[3]['directive'], 'bottled')
        # Restart continues from the retained parcels instead of resetting them.
        self.assertEqual(rows[4]['directive'], 'boil')
        self.assertEqual(rows[4]['sim_cfu'], 200.0)
        rejected = one(self.db,
                       "SELECT * FROM flow_action WHERE id='ACT-C-STOP-UNAPPROVED'")
        self.assertEqual(rejected['approved'], 0)

    def test_qc_failed_sample_cannot_lift_an_order(self):
        self.load('water-flow-route-alpha.json')
        rows = self.directives('tower-a')
        after_qc = [r for r in rows if r['minute'] >= 4]
        self.assertTrue(any('qc-failed' in r['basis'] for r in after_qc))
        self.assertTrue(all(r['directive'] != 'direct' for r in rows[4:10]))

    def test_sample_received_before_sampling_is_atomically_rejected(self):
        self.load_base()
        with self.assertRaises(ValueError):
            import_file(self.db, 'water-flow-import', FIX / 'water-flow-bad-order.json')
        self.assertEqual(
            one(self.db, "SELECT COUNT(*) AS n FROM flow_node WHERE building='tower-x'")['n'], 0)
        self.assertEqual(
            one(self.db, "SELECT COUNT(*) AS n FROM flow_plan WHERE building='tower-x'")['n'], 0)

    def test_action_on_non_controllable_node_is_rejected(self):
        self.load_base()
        with self.assertRaises(ValueError):
            import_file(self.db, 'water-flow-import', FIX / 'water-flow-bad-action.json')

    def test_vacant_tap_directive_goes_to_property_list_not_a_household(self):
        events = [
            {'id': 'PLAN-V', 'kind': 'flow_plan', 'building': 'tower-v', 'horizon': 2,
             'threshold': 100, 'title': 'vacant unit routing'},
            {'id': 'tank-v', 'kind': 'flow_tank', 'building': 'tower-v',
             'initial_volume': 100, 'capacity': 100, 'initial_cfu': 200, 'clean_inflow': 20},
            {'id': 'pipe-v-u2', 'kind': 'flow_pipe', 'building': 'tower-v',
             'upstream_id': 'tank-v', 'pipe_volume': 40, 'initial_volume': 40, 'initial_cfu': 140},
            {'id': 'FTAP-U2', 'kind': 'flow_tap', 'building': 'tower-v', 'tap_id': 'TAP-U2',
             'upstream_id': 'pipe-v-u2', 'demand': 20},
            {'id': 'S-V0', 'kind': 'flow_sample', 'building': 'tower-v',
             'tap_node_id': 'FTAP-U2', 'sampled_minute': 0, 'received_minute': 0,
             'cfu_per_litre': 140, 'qc': 'pass'}]
        path = Path(self.temp.name) / 'vacant.json'
        path.write_text(json.dumps({'events': events}))
        self.load_base()
        import_file(self.db, 'water-flow-import', path)
        row = one(self.db,
                  "SELECT * FROM flow_directive WHERE building='tower-v' AND minute=0")
        self.assertEqual(row['notify_kind'], 'property_vacant')
        self.assertEqual(row['notify_target'], 'PROPERTY:U2')
        self.assertEqual(row['unit_id'], 'U2')

    def test_mass_and_volume_conservation_at_every_node(self):
        for name in ('water-flow-route-alpha.json', 'water-flow-route-beta.json',
                     'water-flow-pump-outage.json'):
            db = connect(':memory:')
            try:
                for command, fixture in BASE:
                    import_file(db, command, FIX / fixture)
                import_file(db, 'water-flow-import', FIX / name)
                worst = one(db, 'SELECT MAX(ABS(conservation_diff)) AS d FROM flow_minute')
                self.assertEqual(worst['d'], 0.0)
                summary = report(db)['flow_conservation']
                self.assertTrue(summary)
                self.assertEqual(summary[0]['max_abs_diff'], 0.0)
            finally:
                db.close()

    def test_audit_replay_is_identical_and_id_conflict_is_rejected(self):
        self.load('water-flow-route-alpha.json')
        result = audit(self.db, HANDLERS)
        self.assertTrue(result['replay_ok'])
        self.assertEqual(result['mismatches'], [])
        path = Path(self.temp.name) / 'conflict.json'
        path.write_text(json.dumps({'events': [
            {'id': 'S-A0', 'kind': 'flow_sample', 'building': 'tower-a',
             'tap_node_id': 'FTAP-U1', 'sampled_minute': 0, 'received_minute': 0,
             'cfu_per_litre': 999, 'qc': 'pass'}]}))
        with self.assertRaises(ValueError):
            import_file(self.db, 'water-flow-import', path)

    def test_later_alert_sample_re_arms_boil_after_clearance(self):
        self.load('water-flow-route-beta.json')
        path = Path(self.temp.name) / 'realert.json'
        path.write_text(json.dumps({'events': [
            {'id': 'S-B8-REALERT', 'kind': 'flow_sample', 'building': 'tower-b',
             'tap_node_id': 'FTAP-U1', 'sampled_minute': 8, 'received_minute': 8,
             'cfu_per_litre': 150, 'qc': 'pass'}]}))
        import_file(self.db, 'water-flow-import', path)
        rows = {r['minute']: r for r in self.directives('tower-b')}
        self.assertEqual(rows[7]['directive'], 'direct')
        self.assertEqual(rows[8]['directive'], 'boil')
        self.assertIn('S-B8-REALERT', rows[8]['basis'])

    def test_repeated_import_is_idempotent(self):
        first = self.load('water-flow-route-beta.json')
        self.assertTrue(any(x['result'] != 'event_replayed' for x in first))
        second = import_file(self.db, 'water-flow-import',
                             FIX / 'water-flow-route-beta.json')
        self.assertEqual(second, [{'result': 'file_replayed'}])
        self.assertEqual(
            one(self.db, "SELECT COUNT(*) AS n FROM flow_minute WHERE building='tower-b'")['n'],
            4 * 9)


if __name__ == '__main__':
    unittest.main()
