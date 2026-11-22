import json
import tempfile
import unittest
from pathlib import Path

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit, report
from housingqueue.store import connect

ROOT = Path(__file__).resolve().parents[1]

SETUP = [
    ('application-import', 'ride-h1-application.json'),
    ('eligibility-import', 'ride-h1-eligibility.json'),
    ('allocation-import', 'ride-h1-allocation.json'),
    ('application-import', 'ride-h2-application.json'),
    ('eligibility-import', 'ride-h2-eligibility.json'),
    ('allocation-import', 'ride-h2-allocation.json'),
]


class RouteCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'routes.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def fixture(self, command, name):
        return import_file(self.db, command, ROOT / 'fixtures' / name)

    def setup_housing(self, which='h1'):
        for command, name in SETUP:
            if which != 'both' and f'-{which}-' not in name:
                continue
            self.fixture(command, name)

    def events(self, command, events):
        path = Path(self.temp.name) / 'events.json'
        path.write_text(json.dumps({'events': events}))
        return import_file(self.db, command, path)

    def route_report(self, request_id='R1'):
        rows = report(self.db)['ride_route']
        return next(r for r in rows if r['request_id'] == request_id)


class DirectItinerary(RouteCase):
    def test_direct_route_catches_original_appointment(self):
        self.setup_housing('h1')
        self.fixture('ride-route-import', 'ride-route-direct.json')
        r = self.route_report('R1')
        legs = [(x['from_place'], x['to_place'], x['arrive_at']) for x in r['legs']]
        self.assertEqual(legs, [('garage', 'h1-door', '2026-05-01T09:10:00'),
                                ('h1-door', 'hospital', '2026-05-01T09:30:00')])
        self.assertEqual([x['status'] for x in r['legs']], ['travelled', 'travelled'])
        household = r['households'][0]
        self.assertEqual(household['alighted_at'], '2026-05-01T09:30:00')
        self.assertEqual(household['caught_original'], 1)
        self.assertEqual(household['verdict'], 'fulfilled')

    def test_wheelchair_and_segment_occupancy_recorded(self):
        self.setup_housing('h1')
        self.fixture('ride-route-import', 'ride-route-direct.json')
        r = self.route_report('R1')
        self.assertEqual([x['wheelchair_in_vehicle'] for x in r['legs']], [0, 1])
        self.assertEqual([x['passengers_in_vehicle'] for x in r['legs']], [0, 1])
        self.assertEqual(len(r['securement']), 2)
        self.assertEqual([x['kind'] for x in r['securement']], ['secure', 'release'])

    def test_audit_replays_route_from_raw_files(self):
        self.setup_housing('h1')
        self.fixture('ride-route-import', 'ride-route-direct.json')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])


class SecondItinerary(RouteCase):
    def test_h2_first_serves_h2_on_time_and_h1_late(self):
        self.setup_housing('both')
        self.fixture('ride-route-import', 'ride-route-h2-first.json')
        r = self.route_report('R2')
        stops = [(x['from_place'], x['to_place'], x['arrive_at']) for x in r['legs']]
        self.assertEqual(stops, [('garage', 'h2-door', '2026-05-01T09:05:00'),
                                 ('h2-door', 'library', '2026-05-01T09:20:00'),
                                 ('library', 'h1-door', '2026-05-01T09:40:00'),
                                 ('h1-door', 'hospital', '2026-05-01T10:00:00')])
        by_household = {x['household_id']: x for x in r['households']}
        self.assertEqual(by_household['H2']['caught_original'], 1)
        self.assertEqual(by_household['H2']['verdict'], 'fulfilled')
        self.assertEqual(by_household['H1']['caught_original'], 0)
        self.assertEqual(by_household['H1']['verdict'], 'late')
        self.assertEqual(by_household['H1']['original_window_end'], '2026-05-01T09:45:00')
        self.assertEqual(by_household['H1']['authorized_window_end'], '2026-05-01T10:15:00')

    def test_route_order_is_not_reservation_order(self):
        """Stops are ordered by physical travel, never by from_at instant travel."""
        self.setup_housing('both')
        self.fixture('ride-route-import', 'ride-route-h2-first.json')
        r = self.route_report('R2')
        self.assertEqual(r['legs'][0]['to_place'], 'h2-door')
        self.assertNotEqual(r['legs'][0]['to_place'], 'h1-door')


class SeatEvidence(RouteCase):
    def test_missing_alighting_keeps_seat_occupied(self):
        """Without a witnessed alight, H2's seat is never silently reused."""
        self.setup_housing('both')
        self.fixture('ride-route-import', 'ride-route-missing-alight.json')
        r = self.route_report('R2')
        by_household = {x['household_id']: x for x in r['households']}
        for household in ('H1', 'H2'):
            self.assertEqual(by_household[household]['alighted_at'], None)
            self.assertEqual(by_household[household]['verdict'], 'partial')
            self.assertIn('no witnessed alighting', by_household[household]['reason'])
        # H2 boarded; no alighting witness exists, so H2 keeps the seat.
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM ride_boarding WHERE kind='alight'").fetchone()[0], 0)
        self.assertGreaterEqual(
            self.db.execute("SELECT COUNT(*) FROM ride_boarding WHERE kind='board'").fetchone()[0], 1)

    def test_boarding_without_released_seat_is_refused(self):
        """A single-seat vehicle cannot carry H1 while H2's seat is unreleased."""
        self.setup_housing('both')
        self.db.execute("UPDATE shuttle_vehicle SET seats=1 WHERE id='V1'")
        self.db.commit()
        events = json.loads((ROOT / 'fixtures' / 'ride-route-missing-alight.json').read_text())['events']
        for e in events:
            if e['id'] == 'R2':
                e['seats'] = 1
                e['wheelchair_places'] = 0
        path = Path(self.temp.name) / 'one-seat.json'
        path.write_text(json.dumps({'events': events}))
        import_file(self.db, 'ride-route-import', path)
        with self.assertRaises(ValueError) as ctx:
            self.events('ride-route-import', [
                {'id': 'H1-HOP', 'kind': 'boarding', 'request_id': 'R2', 'household_id': 'H1',
                 'stop_seq': 3, 'move': 'board', 'at': '2026-05-01T09:45:00',
                 'witness_a': 'driver', 'witness_b': 'escort'}])
        self.assertIn('still occupies a seat', str(ctx.exception))

    def test_two_distinct_witnesses_required(self):
        self.setup_housing('h1')
        self.fixture('ride-route-import', 'ride-route-direct.json')
        with self.assertRaises(ValueError):
            self.events('ride-route-import', [
                {'id': 'BAD', 'kind': 'boarding', 'request_id': 'R1', 'household_id': 'H1',
                 'stop_seq': 1, 'move': 'board', 'at': '2026-05-01T09:12:00',
                 'witness_a': 'same', 'witness_b': 'same'}])

    def test_wheelchair_requires_securement_before_boarding(self):
        self.setup_housing('h1')
        events = json.loads((ROOT / 'fixtures' / 'ride-route-direct.json').read_text())['events']
        events = [e for e in events if e['id'] not in ('R1-SEC', 'R1-REL')]
        path = Path(self.temp.name) / 'no-secure.json'
        path.write_text(json.dumps({'events': events}))
        with self.assertRaises(ValueError):
            import_file(self.db, 'ride-route-import', path)


class ClosureReplanning(RouteCase):
    def test_closure_after_library_delivery_keeps_done_stop(self):
        self.setup_housing('both')
        self.fixture('ride-route-import', 'ride-route-closure.json')
        r = self.route_report('R2')
        self.assertEqual(r['booking']['state'], 'infeasible')
        self.assertEqual(r['legs'][0]['to_place'], 'h2-door')
        h2 = next(x for x in r['households'] if x['household_id'] == 'H2')
        self.assertEqual(h2['alighted_at'], '2026-05-01T09:20:00')
        self.assertEqual(h2['verdict'], 'fulfilled')
        h1 = next(x for x in r['households'] if x['household_id'] == 'H1')
        self.assertEqual(h1['verdict'], 'unreachable')
        self.assertEqual([x['state'] for x in r['stops']], ['boarded', 'alighted', 'unreachable', 'unreachable'])

    def test_closure_replans_only_unrun_tail(self):
        self.setup_housing('h1')
        self.fixture('ride-route-import', 'ride-route-replan.json')
        r = self.route_report('R1')
        self.assertEqual(r['booking']['state'], 'replanned')
        self.assertEqual(r['legs'][0]['status'], 'travelled')
        self.assertEqual(r['legs'][0]['to_place'], 'h1-door')
        tail = [(x['from_place'], x['to_place']) for x in r['legs'] if x['status'] == 'planned']
        self.assertEqual(tail, [('h1-door', 'library'), ('library', 'hospital')])
        self.assertEqual(r['boardings'][0]['at'], '2026-05-01T09:10:00')


class HousingGate(RouteCase):
    def test_signed_but_undelivered_household_is_never_booked(self):
        self.setup_housing('h1')
        self.db.execute("DELETE FROM housing_delivery")
        self.db.commit()
        events = json.loads((ROOT / 'fixtures' / 'ride-route-direct.json').read_text())['events']
        events = [e for e in events if e['kind'] != 'execute']
        path = Path(self.temp.name) / 'no-delivery.json'
        path.write_text(json.dumps({'events': events}))
        with self.assertRaises(ValueError):
            import_file(self.db, 'ride-route-import', path)

    def test_consultation_never_promises_a_seat(self):
        self.setup_housing('h1')
        self.db.execute("DELETE FROM housing_delivery")
        self.db.commit()
        events = [
            {'id': 'V1', 'kind': 'shuttle_vehicle', 'seats': 4, 'wheelchair_places': 1},
            {'id': 'g', 'kind': 'place', 'id': 'garage'},
            {'id': 'gh', 'kind': 'place', 'id': 'h1-door'},
            {'id': 'SG', 'kind': 'stop', 'id': 'ST-GARAGE', 'place_id': 'garage'},
            {'id': 'SH', 'kind': 'stop', 'id': 'ST-H1', 'place_id': 'h1-door'},
            {'id': 'D1', 'kind': 'driver', 'id': 'DRV-1', 'max_drive_minutes': 480,
             'max_continuous_minutes': 240, 'required_rest_minutes': 15},
            {'id': 'SH1', 'kind': 'shift', 'id': 'SHIFT-1', 'driver_id': 'DRV-1', 'vehicle_id': 'V1',
             'starts_at': '2026-05-01T08:30:00', 'ends_at': '2026-05-01T13:00:00'},
            {'id': 'REQ', 'kind': 'ride_request', 'id': 'REQ', 'vehicle_id': 'V1', 'shift_id': 'SHIFT-1',
             'driver_id': 'DRV-1', 'origin_stop': 'ST-GARAGE', 'starts_at': '2026-05-01T09:00:00',
             'seats': 4, 'wheelchair_places': 1, 'context_only': True},
            {'id': 'REQ-S1', 'kind': 'ride_stop', 'request_id': 'REQ', 'seq': 1, 'household_id': 'H1',
             'stop_id': 'ST-H1', 'stop_kind': 'pickup', 'earliest': '2026-05-01T09:00:00',
             'latest': '2026-05-01T09:30:00', 'wheelchair': False},
            {'id': 'CON', 'kind': 'consult', 'id': 'CON', 'request_id': 'REQ', 'household_id': 'H1',
             'at': '2026-05-01T08:00:00'},
        ]
        self.events('ride-route-import', events)
        row = self.db.execute("SELECT answer FROM ride_consultation WHERE id='CON'").fetchone()
        self.assertEqual(row[0], 'consultation_only')
        self.assertEqual(report(self.db)['ride_booking'], [])

    def test_delivery_changes_bookable_set(self):
        from housingqueue.shuttle import bookable_households
        self.setup_housing('both')
        before = bookable_households(self.db, '2026-05-01T09:00:00')
        self.assertEqual(before, ['H1', 'H2'])
        self.db.execute("DELETE FROM housing_delivery WHERE household_id='H2'")
        self.db.commit()
        self.assertEqual(bookable_households(self.db, '2026-05-01T09:00:00'), ['H1'])


class Integrity(RouteCase):
    def test_driver_continuous_limit_forces_recorded_rest(self):
        self.setup_housing('h1')
        events = json.loads((ROOT / 'fixtures' / 'ride-route-direct.json').read_text())['events']
        for e in events:
            if e['id'] == 'DRV-1':
                e['max_continuous_minutes'] = 15
                e['required_rest_minutes'] = 20
        events = [e for e in events if e.get('kind') != 'boarding']
        path = Path(self.temp.name) / 'rest.json'
        path.write_text(json.dumps({'events': events}))
        import_file(self.db, 'ride-route-import', path)
        r = self.route_report('R1')
        # garage->H1-door is 10 minutes; H1-door->hospital (20) would cross the
        # 15 minute continuous cap, so a 20 minute rest must appear.
        breaks = [x['break_minutes'] for x in r['legs']]
        self.assertIn(20, breaks)
        self.assertEqual(r['legs'][1]['depart_at'], '2026-05-01T09:30:00')
        self.assertEqual(r['legs'][1]['arrive_at'], '2026-05-01T09:50:00')

    def test_route_past_shift_end_is_refused(self):
        self.setup_housing('h1')
        events = json.loads((ROOT / 'fixtures' / 'ride-route-direct.json').read_text())['events']
        for e in events:
            if e['id'] == 'SHIFT-1':
                e['ends_at'] = '2026-05-01T09:20:00'
        path = Path(self.temp.name) / 'short.json'
        path.write_text(json.dumps({'events': events}))
        with self.assertRaises(ValueError) as ctx:
            import_file(self.db, 'ride-route-import', path)
        self.assertIn('driver shift', str(ctx.exception))

    def test_vehicle_cannot_be_at_two_stops(self):
        self.setup_housing('h1')
        self.fixture('ride-route-import', 'ride-route-direct.json')
        with self.assertRaises(ValueError) as ctx:
            self.events('ride-route-import', [
                {'id': 'R9', 'kind': 'ride_request', 'id': 'R9', 'vehicle_id': 'V1', 'shift_id': 'SHIFT-1',
                 'driver_id': 'DRV-1', 'origin_stop': 'ST-GARAGE', 'starts_at': '2026-05-01T09:00:00',
                 'seats': 2, 'wheelchair_places': 0},
                {'id': 'R9-S1', 'kind': 'ride_stop', 'request_id': 'R9', 'seq': 1, 'household_id': 'H1',
                 'stop_id': 'ST-H2', 'stop_kind': 'pickup', 'earliest': '2026-05-01T09:00:00',
                 'latest': '2026-05-01T09:30:00', 'wheelchair': False},
                {'id': 'R9-S2', 'kind': 'ride_stop', 'request_id': 'R9', 'seq': 2, 'household_id': 'H1',
                 'stop_id': 'ST-LIB', 'stop_kind': 'dropoff', 'earliest': '2026-05-01T09:00:00',
                 'latest': '2026-05-01T09:30:00', 'wheelchair': False},
                {'id': 'R9-A1', 'kind': 'authorization', 'id': 'R9-A1', 'request_id': 'R9',
                 'household_id': 'H1', 'window_start': '2026-05-01T09:00:00',
                 'window_end': '2026-05-01T09:30:00', 'auth_kind': 'original', 'recorded_by': 'H1'},
                {'id': 'R9-EX', 'kind': 'execute', 'request_id': 'R9', 'at': '2026-05-01T08:00:00'}])
        self.assertIn('one vehicle cannot be at two stops', str(ctx.exception))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM ride_booking WHERE request_id='R9'").fetchone()[0], 0)

    def test_event_id_conflict_is_atomic(self):
        self.setup_housing('h1')
        path = ROOT / 'fixtures' / 'ride-route-direct.json'
        self.fixture('ride-route-import', 'ride-route-direct.json')
        before = report(self.db)
        forged = json.loads(path.read_text())
        forged['events'] = [e for e in forged['events'] if e['kind'] in ('place', 'stop')]
        forged['events'][0]['id'] = 'RG-1'
        forged['events'][0]['kind'] = 'segment'
        forged['events'][0].update({'from_place': 'garage', 'to_place': 'h1-door', 'minutes': 99})
        conflict = Path(self.temp.name) / 'conflict.json'
        conflict.write_text(json.dumps(forged))
        with self.assertRaises(ValueError):
            import_file(self.db, 'ride-route-import', conflict)
        after = report(self.db)
        self.assertEqual(before['ride_leg'], after['ride_leg'])
        self.assertEqual(before['source_file'], after['source_file'])
        self.assertEqual(before['ride_segment'] if 'ride_segment' in before else before['shuttle_segment'],
                         after['shuttle_segment'])

    def test_legacy_shuttle_still_works(self):
        for command, name in [('application-import', 'application-ok.json'),
                              ('eligibility-import', 'eligibility-ok.json'),
                              ('allocation-import', 'allocation-ok.json'),
                              ('shuttle-import', 'shuttle-ok.json')]:
            self.fixture(command, name)
        state = self.db.execute("SELECT state FROM shuttle_decision WHERE request_id='TRIP-1'").fetchone()[0]
        self.assertEqual(state, 'booked')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_audit_detects_forged_leg(self):
        self.setup_housing('h1')
        self.fixture('ride-route-import', 'ride-route-direct.json')
        self.db.execute("UPDATE ride_leg SET to_place='library' WHERE seq=2")
        self.db.commit()
        result = audit(self.db, HANDLERS)
        self.assertFalse(result['replay_ok'])
        self.assertIn('ride_leg', result['mismatches'])


if __name__ == '__main__':
    unittest.main()
