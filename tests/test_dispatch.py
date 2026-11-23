import json
import tempfile
import unittest
from pathlib import Path

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import report, audit
from housingqueue.store import connect

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'fixtures'


def seed_occupied_unit(db, unit_id, community='ward-t'):
    """Insert a unit plus the offer/delivery rows that make it an occupied load."""
    db.execute("INSERT INTO unit(id,community,rooms,accessible,available_at,state) "
               "VALUES(?,?,'1',0,'2026-04-01T11:00:00','occupied')", (unit_id, community))
    db.execute("INSERT INTO offer(id,household_id,unit_id,offered_at,expires_at,state,reason,eligibility_version)"
               " VALUES(?,?,?,'2026-04-01T12:00:00','2026-04-02T12:00:00','signed','test',1)",
               ('O-' + unit_id, 'H-' + unit_id, unit_id))
    db.execute("INSERT INTO delivery(id,offer_id,unit_id,household_id,at,witness,evidence,state)"
               " VALUES(?,?,?,?,'2026-04-03T12:00:00','test-witness','test-evidence.jpg','delivered')",
               ('DLV-' + unit_id, 'O-' + unit_id, unit_id, 'H-' + unit_id))

BASE_CHAIN = [
    ('application-import', 'application-ok.json'),
    ('eligibility-import', 'eligibility-ok.json'),
    ('allocation-import', 'allocation-ok.json'),
    ('application-import', 'application-dispatch.json'),
    ('eligibility-import', 'eligibility-dispatch.json'),
    ('allocation-import', 'allocation-dispatch.json'),
]


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'dispatch.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def load(self, chain):
        for command, name in chain:
            import_file(self.db, command, F / name)

    def segments(self, plan_id):
        rows = self.db.execute(
            'SELECT * FROM supply_segment WHERE plan_id=? ORDER BY segment_no', (plan_id,)).fetchall()
        return [dict(r) for r in rows]

    def test_two_feasible_timelines_with_different_served_households(self):
        self.load(BASE_CHAIN + [
            ('allocation-import', 'allocation-sign-u3-early.json'),
            ('dispatch-import', 'dispatch-full-occupancy.json'),
            ('dispatch-import', 'dispatch-window-full.json'),
        ])
        plans = [dict(r) for r in self.db.execute(
            'SELECT id,seq,adopted FROM supply_plan ORDER BY seq')]
        # two feasible timelines are stored, not one, and not keyed/ordered by household id
        self.assertEqual([p['id'] for p in plans], ['WIN-FULL-P1', 'WIN-FULL-P2'])
        self.assertEqual([p['adopted'] for p in plans], [1, 0])

        p1, p2 = self.segments('WIN-FULL-P1'), self.segments('WIN-FULL-P2')
        # plan 1: U2 freezer protected first half, U3 pump shed
        first1 = {tuple(json.loads(s['powered_devices'])) for s in p1[:6]}
        self.assertEqual(first1, {('D1-U1', 'D1-U2')})
        second1 = {tuple(json.loads(s['powered_devices'])) for s in p1[6:]}
        self.assertEqual(second1, {('D1-U1',)})
        # plan 2: U3 pump protected second half, U2 freezer shed
        first2 = {tuple(json.loads(s['powered_devices'])) for s in p2[:6]}
        self.assertEqual(first2, {('D1-U1',)})
        second2 = {tuple(json.loads(s['powered_devices'])) for s in p2[6:]}
        self.assertEqual(second2, {('D1-U1', 'D1-U3')})

        for plan in (p1, p2):
            # life-support U1 powered on every slice; battery continuous and never negative
            self.assertTrue(all('D1-U1' in json.loads(s['powered_devices']) for s in plan))
            self.assertEqual([s['battery_before_ws'] for s in plan][1:],
                             [s['battery_after_ws'] for s in plan][:-1])
            self.assertGreaterEqual(plan[-1]['battery_after_ws'], 0)
            # instantaneous inverter limit holds on every 5-minute slice
            self.assertTrue(all(s['power_w'] <= s['limit_w'] for s in plan))
        # both sequences fully spend the 1.5 kWh (5 400 000 Ws)
        self.assertEqual(p1[-1]['battery_after_ws'], 0)
        self.assertEqual(p2[-1]['battery_after_ws'], 0)
        # unfulfilled commitments are named per segment
        unfulfilled_u3 = [u for s in p1[6:] for u in json.loads(s['unfulfilled'])]
        self.assertTrue(all(u['device_id'] == 'D1-U3' for u in unfulfilled_u3))
        unfulfilled_u2 = [u for s in p2[:6] for u in json.loads(s['unfulfilled'])]
        self.assertTrue(all(u['device_id'] == 'D1-U2' for u in unfulfilled_u2))

    def test_minimum_hold_and_slice_level_inverter_enforcement(self):
        self.load(BASE_CHAIN + [
            ('allocation-import', 'allocation-sign-u3-early.json'),
            ('dispatch-import', 'dispatch-full-occupancy.json'),
            ('dispatch-import', 'dispatch-window-full.json'),
        ])
        for pid in ('WIN-FULL-P1', 'WIN-FULL-P2'):
            plan = self.segments(pid)
            for device in ('D1-U2', 'D1-U3'):
                on = [s['segment_no'] for s in plan if device in json.loads(s['powered_devices'])]
                if on:
                    # once energized, an ordinary load stays on a contiguous full 30-minute block
                    self.assertEqual(on, list(range(on[0], on[-1] + 1)))
                    self.assertEqual(len(on), 6)
            # no slice may exceed the inverter even though hourly kWh would fit
            self.assertTrue(all(s['power_w'] <= 2000 for s in plan))

    def test_undelivered_signed_unit_is_not_demand_and_delivery_replans(self):
        self.load(BASE_CHAIN)
        # U3 signed but not delivered: it must not count as an occupied load
        self.load([('allocation-import', 'dispatch-sign-u3.json')])
        self.assertEqual(self.db.execute(
            "SELECT state FROM unit WHERE id='U3'").fetchone()[0], 'assigned')
        self.assertEqual(dict(self.db.execute(
            "SELECT * FROM power_demand WHERE community='ward-a'").fetchone()),
            {'community': 'ward-a', 'total_kw_tenths': 0, 'reading_count': 0, 'state': 'pending'})

        self.load([('dispatch-import', 'dispatch-setup.json'),
                   ('dispatch-import', 'dispatch-window.json')])
        # only U1/U2 are occupied, so exactly one feasible timeline exists
        self.assertEqual([r[0] for r in self.db.execute(
            'SELECT id FROM supply_plan ORDER BY seq')], ['WIN-1-P1'])

        # witness the 18:00-18:20 switches of the adopted plan
        self.load([('dispatch-import', 'dispatch-switches.json')])
        executed_before = [tuple(r) for r in self.db.execute(
            "SELECT segment_no,powered_devices,battery_after_ws FROM supply_segment "
            "WHERE state='executed' ORDER BY segment_no")]

        # physical delivery of U3 at 18:20 makes it occupied
        self.load([('dispatch-import', 'dispatch-deliver-u3.json')])
        self.assertEqual(self.db.execute(
            "SELECT state FROM unit WHERE id='U3'").fetchone()[0], 'occupied')
        executed_after = [tuple(r) for r in self.db.execute(
            "SELECT segment_no,powered_devices,battery_after_ws FROM supply_segment "
            "WHERE state='executed' ORDER BY segment_no")]
        # already executed U1/U2 segments are retained byte-for-byte
        self.assertEqual(executed_before, executed_after)

        plans = [r[0] for r in self.db.execute('SELECT id FROM supply_plan ORDER BY seq')]
        self.assertEqual(plans, ['WIN-1-P1', 'WIN-1-P2'])
        # the newly feasible P2 includes U3 in the second half, not before 18:30
        p2 = self.segments('WIN-1-P2')
        self.assertTrue(all('DEV-U3' not in json.loads(s['powered_devices']) for s in p2[:6]))
        self.assertTrue(all('DEV-U3' in json.loads(s['powered_devices']) for s in p2[6:]))
        # battery state remains continuous across the 18:20 boundary on P1
        p1 = self.segments('WIN-1-P1')
        self.assertEqual(p1[3]['battery_after_ws'], p1[4]['battery_before_ws'])

    def test_late_meter_reading_never_overwrites_executed_segments(self):
        self.load(BASE_CHAIN + [
            ('dispatch-import', 'dispatch-setup.json'),
            ('dispatch-import', 'dispatch-window.json'),
            ('dispatch-import', 'dispatch-switches.json'),
        ])
        snapshot = [tuple(r) for r in self.db.execute(
            "SELECT segment_no,powered_devices,battery_after_ws,state FROM supply_segment "
            "WHERE plan_id='WIN-1-P1' ORDER BY segment_no")]
        # a later meter reading arrives after the first switches were witnessed
        import_file(self.db, 'power-import', F / 'power-ok.json')
        after = [tuple(r) for r in self.db.execute(
            "SELECT segment_no,powered_devices,battery_after_ws,state FROM supply_segment "
            "WHERE plan_id='WIN-1-P1' ORDER BY segment_no")]
        self.assertEqual(snapshot, after)
        self.assertEqual([r[0] for r in self.db.execute(
            "SELECT state FROM supply_segment WHERE state='executed'")],
                         ['executed'] * 4)

    def test_adopt_switches_the_used_timeline(self):
        self.load(BASE_CHAIN + [
            ('allocation-import', 'allocation-sign-u3-early.json'),
            ('dispatch-import', 'dispatch-full-occupancy.json'),
            ('dispatch-import', 'dispatch-window-full.json'),
            ('dispatch-import', 'dispatch-adopt-p2.json'),
        ])
        flags = {r[0]: r[1] for r in self.db.execute('SELECT id,adopted FROM supply_plan')}
        self.assertEqual(flags, {'WIN-FULL-P1': 0, 'WIN-FULL-P2': 1})
        self.assertEqual(self.db.execute(
            "SELECT adopt_seq FROM dispatch_window WHERE id='WIN-FULL'").fetchone()[0], 2)

    def test_inverter_overload_rejected_even_when_energy_would_fit(self):
        # 1.8 kW life-support vs 1.5 kW inverter; the battery (3 kWh) is not the binding limit
        with self.assertRaises(ValueError) as ctx:
            self.load([
                ('application-import', 'application-overload.json'),
                ('eligibility-import', 'eligibility-overload.json'),
                ('allocation-import', 'allocation-overload.json'),
                ('dispatch-import', 'dispatch-overload.json'),
                ('dispatch-import', 'dispatch-window-overload.json'),
            ])
        self.assertIn('inverter', str(ctx.exception))
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM supply_plan').fetchone()[0], 0)

    def test_meter_conflict_is_flagged_and_missing_reading_is_not_zero(self):
        self.load(BASE_CHAIN + [
            ('dispatch-import', 'dispatch-setup.json'),
        ])
        # no meter reading at all -> pending, never an implicit zero
        self.assertEqual(self.db.execute(
            "SELECT state FROM power_demand WHERE community='ward-a'").fetchone()[0], 'pending')
        self.load([('power-import', 'power-conflict.json'),
                   ('dispatch-import', 'dispatch-conflict.json')])
        self.assertEqual(self.db.execute(
            "SELECT state FROM device WHERE id='DEV-U1'").fetchone()[0], 'conflict_pending')
        # conflicting device is excluded from scheduling inputs until verified
        self.load([('dispatch-import', 'dispatch-window.json')])
        powered = {d for s in self.db.execute(
            "SELECT powered_devices FROM supply_segment") for d in json.loads(s[0])}
        self.assertNotIn('DEV-U1', powered)

    def test_battery_too_small_for_life_support_is_rejected(self):
        # 1 kW ventilator for one hour needs 1 kWh; a 500 Wh battery cannot sustain it
        from housingqueue import dispatch
        from housingqueue.store import connect as _connect
        db = _connect(':memory:')
        try:
            seed_occupied_unit(db, 'UT', 'ward-t')
            db.execute("INSERT INTO battery(id,community,usable_ws,state,reason) VALUES('B','ward-t',1800000,'available','x')")
            db.execute("INSERT INTO inverter(id,community,limit_w,state,reason) VALUES('I','ward-t',2000,'available','x')")
            db.execute("INSERT INTO device(id,unit_id,label,critical,power_w,interruptible,consent,"
                       "window_start,window_end,state,reason) VALUES('DV','UT','vent',1,1000,0,'none',"
                       "'2026-04-03T18:00:00','2026-04-03T19:00:00','verified','x')")
            db.execute("INSERT INTO dispatch_window(id,community,start_at,end_at,slice_minutes,adopt_seq,state,reason)"
                       " VALUES('W','ward-t','2026-04-03T18:00:00','2026-04-03T19:00:00',5,1,'planned','x')")
            with self.assertRaises(ValueError) as ctx:
                dispatch.plan_window(db, {'id': 'W', 'window_id': 'W', 'community': 'ward-t'})
            self.assertIn('battery', str(ctx.exception))
        finally:
            db.close()

    def test_ordinary_load_holds_full_thirty_minutes_with_ten_minute_slices(self):
        from housingqueue import dispatch
        from housingqueue.store import connect as _connect
        db = _connect(':memory:')
        try:
            seed_occupied_unit(db, 'UA', 'ward-z')
            db.execute("INSERT INTO battery(id,community,usable_ws,state,reason) VALUES('BZ','ward-z',5400000,'available','x')")
            db.execute("INSERT INTO inverter(id,community,limit_w,state,reason) VALUES('IZ','ward-z',2000,'available','x')")
            db.execute("INSERT INTO device(id,unit_id,label,critical,power_w,interruptible,consent,"
                       "window_start,window_end,state,reason) VALUES('DZ-C','UA','vent',1,1000,0,'none',"
                       "'2026-04-03T18:00:00','2026-04-03T19:00:00','verified','x')")
            db.execute("INSERT INTO device(id,unit_id,label,critical,power_w,interruptible,consent,"
                       "window_start,window_end,state,reason) VALUES('DZ-O','UA','pump',0,500,1,'written',"
                       "'2026-04-03T18:00:00','2026-04-03T19:00:00','declared','x')")
            db.execute("INSERT INTO dispatch_window(id,community,start_at,end_at,slice_minutes,adopt_seq,state,reason)"
                       " VALUES('WZ','ward-z','2026-04-03T18:00:00','2026-04-03T19:00:00',10,1,'planned','x')")
            dispatch.plan_window(db, {'id': 'WZ', 'window_id': 'WZ', 'community': 'ward-z'})
            segs = [dict(r) for r in db.execute(
                "SELECT * FROM supply_segment WHERE plan_id='WZ-P1' ORDER BY segment_no")]
            on = [s['segment_no'] for s in segs if 'DZ-O' in json.loads(s['powered_devices'])]
            # 30-minute minimum hold over 10-minute slices means exactly three contiguous slices
            self.assertTrue(on)
            self.assertEqual(on, list(range(on[0], on[-1] + 1)))
            self.assertEqual(len(on), 3)
        finally:
            db.close()

    def test_audit_replay_reproduces_dispatch_state_segment_by_segment(self):
        self.load(BASE_CHAIN + [
            ('allocation-import', 'allocation-sign-u3-early.json'),
            ('dispatch-import', 'dispatch-full-occupancy.json'),
            ('dispatch-import', 'dispatch-window-full.json'),
            ('dispatch-import', 'dispatch-adopt-p2.json'),
        ])
        result = audit(self.db, HANDLERS)
        self.assertTrue(result['replay_ok'], result['mismatches'])
        rep = report(self.db)
        # event/source_file and segment state all appear in the report
        self.assertIn('supply_segment', rep)
        self.assertIn('switch_witness', rep)
        self.assertEqual(len(rep['event']), result['event_count'])


if __name__ == '__main__':
    unittest.main()
