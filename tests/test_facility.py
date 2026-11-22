import json
import tempfile
import unittest
from pathlib import Path

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import report, audit
from housingqueue.store import connect


ROOT = Path(__file__).resolve().parents[1]


class FacilityCase(unittest.TestCase):
    def test_signed_family_reserves_room_and_audit_replays(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('facility-import', 'facility-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            state = db.execute("SELECT state FROM facility_decision WHERE booking_id='FB1'").fetchone()[0]
            self.assertEqual(state, 'reserved')
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()


class ActivityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'test.sqlite')
        for command, name in [('application-import', 'application-facility.json'),
                              ('eligibility-import', 'eligibility-facility.json'),
                              ('allocation-import', 'allocation-facility.json'),
                              ('facility-import', 'facility-evening.json')]:
            import_file(self.db, command, ROOT / 'fixtures' / name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def fixture(self, cmd, name):
        return import_file(self.db, cmd, ROOT / 'fixtures' / name)

    def events(self, cmd, events):
        p = Path(self.temp.name) / 'events.json'
        p.write_text(json.dumps({'events': events}))
        return import_file(self.db, cmd, p)

    def session(self, sid):
        return self.db.execute('SELECT * FROM facility_session WHERE id=?', (sid,)).fetchone()

    def activity(self, aid):
        return self.db.execute('SELECT * FROM activity WHERE id=?', (aid,)).fetchone()

    def checkin(self, cid):
        return self.db.execute('SELECT * FROM facility_checkin WHERE id=?', (cid,)).fetchone()

    def status(self, r, sid):
        return next(x for x in r['facility_session_status'] if x['session_id'] == sid)

    def test_setup_houses_hosts_and_authorizes_activities(self):
        r = report(self.db)
        self.assertEqual([h['status'] for h in r['household']], ['housed', 'housed', 'housed'])
        self.assertEqual([a['state'] for a in r['activity']], ['authorized'] * 3)
        self.assertEqual(len(r['activity_window']), 2)
        self.assertEqual(r['guest_pass'][0]['state'], 'authorized')
        self.assertEqual(self.activity('ACT1')['outcome'], 'pending')

    def test_joint_arrangement_keeps_goal_and_postpones_others(self):
        self.fixture('facility-import', 'facility-joint.json')
        r = report(self.db)
        self.assertEqual(self.session('SJ1')['state'], 'scheduled')
        self.assertEqual(self.session('SJ2')['state'], 'conflict')
        self.assertEqual(self.session('SJ3')['state'], 'conflict')
        act1 = self.activity('ACT1')
        self.assertEqual(act1['outcome'], 'joint_achieved')
        self.assertEqual(act1['outcome_session_id'], 'SJ1')
        self.assertEqual(self.activity('ACT2')['outcome'], 'pending')
        self.assertEqual(self.activity('ACT3')['outcome'], 'pending')
        sj1 = self.status(r, 'SJ1')
        self.assertEqual(sj1['held'], 'held')
        self.assertEqual(sj1['valid_checkins'], 6)
        self.assertEqual(sj1['goal_window'], 'primary')
        self.assertTrue(sj1['joint_outcome_session'])
        self.assertEqual({p['source'] for p in sj1['personnel']}, {'member'})
        self.assertEqual(self.checkin('CJG')['state'], 'rejected')
        self.assertIn('full', self.checkin('CJG')['reason'])
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_split_arrangement_loses_joint_goal_but_keeps_schedule(self):
        self.fixture('facility-import', 'facility-split.json')
        r = report(self.db)
        for sid in ('SS1', 'SS2', 'SS3', 'SS4'):
            self.assertEqual(self.session(sid)['state'], 'scheduled', sid)
        act1 = self.activity('ACT1')
        self.assertEqual(act1['outcome'], 'split_held')
        self.assertIsNone(act1['outcome_session_id'])
        self.assertEqual(self.activity('ACT2')['outcome'], 'joint_achieved')
        self.assertEqual(self.activity('ACT2')['outcome_session_id'], 'SS3')
        self.assertEqual(self.activity('ACT3')['outcome_session_id'], 'SS4')
        ss1, ss2 = self.status(r, 'SS1'), self.status(r, 'SS2')
        self.assertEqual(ss1['goal_window'], 'split')
        self.assertEqual(ss1['valid_checkins'], 3)
        self.assertEqual([p['person_id'] for p in ss2['personnel']], ['p4', 'p5', 'g1'])
        guest = next(p for p in ss2['personnel'] if p['person_id'] == 'g1')
        self.assertEqual(guest['source'], 'guest')
        self.assertEqual(guest['late'], 1)
        self.assertEqual(self.checkin('CS4')['state'], 'rejected')
        self.assertIn('full', self.checkin('CS4')['reason'])
        self.assertEqual(self.checkin('CS5')['state'], 'void')
        self.assertIn('membership or guest authorization', self.checkin('CS5')['reason'])
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_member_correction_withdraws_only_involved_session(self):
        self.fixture('facility-import', 'facility-split.json')
        self.fixture('application-import', 'facility-correction.json')
        r = report(self.db)
        self.assertEqual(self.checkin('CS3')['state'], 'void')
        self.assertEqual(self.session('SS1')['state'], 'clearance_withdrawn')
        ss1 = self.status(r, 'SS1')
        self.assertEqual(ss1['valid_checkins'], 2)
        self.assertEqual(ss1['void_checkins'], 2)
        self.assertEqual(ss1['room_responsibility'][0]['state'], 'released')
        self.assertEqual(ss1['room_responsibility'][0]['released_at'], '2026-04-03T18:30:00')
        self.assertEqual(self.session('SS2')['state'], 'scheduled')
        self.assertEqual(self.status(r, 'SS2')['valid_checkins'], 3)
        self.assertEqual(self.status(r, 'SS3')['valid_checkins'], 4)
        self.assertEqual(self.status(r, 'SS4')['valid_checkins'], 4)
        self.assertEqual(self.checkin('CS6')['state'], 'valid')
        self.assertEqual(self.checkin('CS13')['state'], 'valid')
        self.assertEqual(self.activity('ACT1')['outcome'], 'split_held')
        self.assertEqual(self.activity('ACT2')['outcome'], 'joint_achieved')
        self.assertEqual(r['household'][0]['status'], 'housed')
        self.assertEqual(r['member'][2]['state'], 'removed')
        self.assertEqual(r['member'][2]['removed_at'], '2026-04-03T18:30:00')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_attendee_correction_event_voids_guest_and_withdraws_clearance(self):
        self.fixture('facility-import', 'facility-split.json')
        self.events('facility-import', [{'id': 'AC1', 'kind': 'attendee_correction', 'activity_id': 'ACT1',
                                         'person_id': 'g1', 'at': '2026-04-03T19:30:00',
                                         'reviewer': 'staff-1', 'reason': 'guest pass issued in error'}])
        self.assertEqual(self.checkin('CS12')['state'], 'void')
        self.assertEqual(self.session('SS2')['state'], 'clearance_withdrawn')
        self.assertEqual(self.session('SS1')['state'], 'scheduled')
        self.assertEqual(self.checkin('CS1')['state'], 'valid')

    def test_remove_member_requires_active_member(self):
        with self.assertRaises(ValueError):
            self.events('application-import', [{'id': 'MX', 'kind': 'remove_member', 'person_id': 'nobody',
                                                'household_id': 'h1', 'at': '2026-04-03T18:30:00'}])

    def test_one_person_one_session_per_slot(self):
        self.events('facility-import', [
            {'id': 'ACTZ', 'kind': 'activity', 'household_id': 'h1', 'title': 'prep', 'attendees': 2,
             'starts': '2026-04-03T18:00:00', 'ends': '2026-04-03T19:00:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SZ1', 'kind': 'session', 'activity_id': 'ACTZ', 'room_id': 'R2',
             'starts': '2026-04-03T18:00:00', 'ends': '2026-04-03T19:00:00', 'attendees': 2},
            {'id': 'ACTY', 'kind': 'activity', 'household_id': 'h1', 'title': 'overflow', 'attendees': 2,
             'starts': '2026-04-03T18:30:00', 'ends': '2026-04-03T19:30:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SZ2', 'kind': 'session', 'activity_id': 'ACTY', 'room_id': 'R1',
             'starts': '2026-04-03T18:30:00', 'ends': '2026-04-03T19:30:00', 'attendees': 2},
            {'id': 'CZ1', 'kind': 'checkin', 'session_id': 'SZ1', 'person_id': 'p2',
             'at': '2026-04-03T18:00:00', 'witness': 'p1', 'witness_kind': 'host'},
            {'id': 'CZ2', 'kind': 'checkin', 'session_id': 'SZ2', 'person_id': 'p2',
             'at': '2026-04-03T18:40:00', 'witness': 'p1', 'witness_kind': 'host'}])
        self.assertEqual(self.session('SZ2')['state'], 'scheduled')
        self.assertEqual(self.checkin('CZ1')['state'], 'valid')
        self.assertEqual(self.checkin('CZ2')['state'], 'rejected')
        self.assertIn('overlapping', self.checkin('CZ2')['reason'])

    def test_witness_must_be_host_applicant_or_independent(self):
        self.events('facility-import', [
            {'id': 'ACTW', 'kind': 'activity', 'household_id': 'h1', 'title': 'talk', 'attendees': 2,
             'starts': '2026-04-05T10:00:00', 'ends': '2026-04-05T11:00:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SW', 'kind': 'session', 'activity_id': 'ACTW', 'room_id': 'R2',
             'starts': '2026-04-05T10:00:00', 'ends': '2026-04-05T11:00:00', 'attendees': 2}])
        self.events('facility-import', [
            {'id': 'CW1', 'kind': 'checkin', 'session_id': 'SW', 'person_id': 'p2', 'at': '2026-04-05T10:00:00',
             'witness': 'p2', 'witness_kind': 'independent'},
            {'id': 'CW2', 'kind': 'checkin', 'session_id': 'SW', 'person_id': 'p3', 'at': '2026-04-05T10:00:00',
             'witness': 'p2', 'witness_kind': 'host'},
            {'id': 'CW3', 'kind': 'checkin', 'session_id': 'SW', 'person_id': 'p4', 'at': '2026-04-05T10:00:00',
             'witness': 'p2', 'witness_kind': 'independent'},
            {'id': 'CW4', 'kind': 'checkin', 'session_id': 'SW', 'person_id': 'p5', 'at': '2026-04-05T10:00:00',
             'witness': 'staff-1', 'witness_kind': 'independent'}])
        states = [self.checkin(c)['state'] for c in ('CW1', 'CW2', 'CW3', 'CW4')]
        self.assertEqual(states, ['rejected', 'rejected', 'rejected', 'valid'])

    def test_equipment_noise_and_access_conditions(self):
        self.events('facility-import', [
            {'id': 'SE1', 'kind': 'session', 'activity_id': 'ACT2', 'room_id': 'R2',
             'starts': '2026-04-03T18:00:00', 'ends': '2026-04-03T19:00:00', 'attendees': 4}])
        self.assertEqual(self.session('SE1')['state'], 'blocked')
        self.assertIn('equipment', self.session('SE1')['reason'])
        self.assertEqual(json.loads(self.session('SE1')['needs_equipment']), ['stage'])
        self.events('facility-import', [
            {'id': 'ACTL', 'kind': 'activity', 'household_id': 'h1', 'title': 'drums', 'attendees': 2,
             'starts': '2026-04-05T20:00:00', 'ends': '2026-04-05T21:00:00', 'noise': 'loud', 'equipment': []},
            {'id': 'SL1', 'kind': 'session', 'activity_id': 'ACTL', 'room_id': 'R2',
             'starts': '2026-04-05T20:00:00', 'ends': '2026-04-05T21:00:00', 'attendees': 2},
            {'id': 'SL2', 'kind': 'session', 'activity_id': 'ACTL', 'room_id': 'R1',
             'starts': '2026-04-05T20:00:00', 'ends': '2026-04-05T21:00:00', 'attendees': 2}])
        self.assertEqual(self.session('SL1')['state'], 'blocked')
        self.assertIn('noise', self.session('SL1')['reason'])
        self.assertEqual(self.session('SL2')['state'], 'scheduled')

    def test_divided_areas_have_own_conditions(self):
        self.events('facility-import', [
            {'id': 'ACTA', 'kind': 'activity', 'household_id': 'h2', 'title': 'reading', 'attendees': 5,
             'starts': '2026-04-05T10:00:00', 'ends': '2026-04-05T11:00:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SA', 'kind': 'session', 'activity_id': 'ACTA', 'room_id': 'R1', 'area_id': 'R1-a',
             'starts': '2026-04-05T10:00:00', 'ends': '2026-04-05T11:00:00', 'attendees': 2},
            {'id': 'SB', 'kind': 'session', 'activity_id': 'ACTA', 'room_id': 'R1', 'area_id': 'R1-b',
             'starts': '2026-04-05T10:00:00', 'ends': '2026-04-05T11:00:00', 'attendees': 2},
            {'id': 'SC', 'kind': 'session', 'activity_id': 'ACTA', 'room_id': 'R1',
             'starts': '2026-04-05T10:00:00', 'ends': '2026-04-05T11:00:00', 'attendees': 2},
            {'id': 'SD', 'kind': 'session', 'activity_id': 'ACTA', 'room_id': 'R1', 'area_id': 'R1-a',
             'starts': '2026-04-05T10:00:00', 'ends': '2026-04-05T11:00:00', 'attendees': 5}])
        self.assertEqual(self.session('SA')['state'], 'scheduled')
        self.assertEqual(self.session('SB')['state'], 'scheduled')
        self.assertEqual(self.session('SC')['state'], 'conflict')
        self.assertEqual(self.session('SD')['state'], 'blocked')
        self.assertIn('capacity', self.session('SD')['reason'])

    def test_session_move_rechecks_conditions_and_keeps_checkins(self):
        self.events('facility-import', [
            {'id': 'R3', 'kind': 'facility_room', 'community': 'ward-b', 'capacity': 1, 'accessible': False},
            {'id': 'ACTM', 'kind': 'activity', 'household_id': 'h1', 'title': 'chat', 'attendees': 2,
             'starts': '2026-04-05T14:00:00', 'ends': '2026-04-05T15:00:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SM', 'kind': 'session', 'activity_id': 'ACTM', 'room_id': 'R2',
             'starts': '2026-04-05T14:00:00', 'ends': '2026-04-05T15:00:00', 'attendees': 2},
            {'id': 'CM1', 'kind': 'checkin', 'session_id': 'SM', 'person_id': 'p1', 'at': '2026-04-05T14:00:00',
             'witness': 'staff-1', 'witness_kind': 'independent'},
            {'id': 'CM2', 'kind': 'checkin', 'session_id': 'SM', 'person_id': 'p2', 'at': '2026-04-05T14:00:00',
             'witness': 'p1', 'witness_kind': 'host'},
            {'id': 'MV1', 'kind': 'session_move', 'session_id': 'SM', 'room_id': 'R3', 'at': '2026-04-05T14:10:00'},
            {'id': 'MV2', 'kind': 'session_move', 'session_id': 'SM', 'room_id': 'R1', 'at': '2026-04-05T14:20:00'}])
        self.assertEqual(self.session('SM')['room_id'], 'R1')
        uses = self.db.execute('SELECT * FROM facility_room_use WHERE session_id=? ORDER BY id', ('SM',)).fetchall()
        self.assertEqual([(u['room_id'], u['state']) for u in uses],
                         [('R2', 'released'), ('R1', 'occupied')])
        self.assertEqual(uses[0]['released_at'], '2026-04-05T14:20:00')
        self.assertEqual(self.checkin('CM1')['state'], 'valid')
        self.assertEqual(self.checkin('CM2')['state'], 'valid')
        blocked = self.db.execute("SELECT state,reason FROM decision_log WHERE object_id='SM' AND state='blocked'").fetchone()
        self.assertIn('capacity', blocked['reason'])

    def test_session_cancel_releases_room_and_held_session_stays(self):
        self.events('facility-import', [
            {'id': 'ACTC', 'kind': 'activity', 'household_id': 'h3', 'title': 'club', 'attendees': 2,
             'starts': '2026-04-05T16:00:00', 'ends': '2026-04-05T17:00:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SC1', 'kind': 'session', 'activity_id': 'ACTC', 'room_id': 'R2',
             'starts': '2026-04-05T16:00:00', 'ends': '2026-04-05T17:00:00', 'attendees': 2},
            {'id': 'XC1', 'kind': 'session_cancel', 'session_id': 'SC1', 'at': '2026-04-05T15:00:00', 'reason': 'host ill'},
            {'id': 'SC2', 'kind': 'session', 'activity_id': 'ACTC', 'room_id': 'R2',
             'starts': '2026-04-05T16:00:00', 'ends': '2026-04-05T17:00:00', 'attendees': 2},
            {'id': 'CC1', 'kind': 'checkin', 'session_id': 'SC2', 'person_id': 'r1', 'at': '2026-04-05T16:00:00',
             'witness': 'staff-1', 'witness_kind': 'independent'},
            {'id': 'XC2', 'kind': 'session_cancel', 'session_id': 'SC2', 'at': '2026-04-05T16:30:00', 'reason': 'too late'}])
        self.assertEqual(self.session('SC1')['state'], 'cancelled')
        self.assertEqual(self.session('SC2')['state'], 'scheduled')
        self.assertEqual(self.checkin('CC1')['state'], 'valid')
        uses = self.db.execute('SELECT * FROM facility_room_use WHERE session_id=? ORDER BY id', ('SC1',)).fetchall()
        self.assertEqual(uses[0]['state'], 'released')

    def test_consent_windows_bound_split_sessions(self):
        self.events('facility-import', [
            {'id': 'SX1', 'kind': 'session', 'activity_id': 'ACT1', 'room_id': 'R2',
             'starts': '2026-04-03T20:00:00', 'ends': '2026-04-03T21:00:00', 'attendees': 3},
            {'id': 'SX2', 'kind': 'session', 'activity_id': 'ACT1', 'room_id': 'R2',
             'starts': '2026-04-03T18:00:00', 'ends': '2026-04-03T19:00:00', 'attendees': 4},
            {'id': 'ACTN', 'kind': 'activity', 'household_id': 'h1', 'title': 'no consent', 'attendees': 4,
             'starts': '2026-04-05T18:00:00', 'ends': '2026-04-05T20:00:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SX3', 'kind': 'session', 'activity_id': 'ACTN', 'room_id': 'R2',
             'starts': '2026-04-05T19:00:00', 'ends': '2026-04-05T20:00:00', 'attendees': 2}])
        self.assertEqual(self.session('SX1')['state'], 'blocked')
        self.assertIn('consented', self.session('SX1')['reason'])
        self.assertEqual(self.session('SX2')['state'], 'blocked')
        self.assertIn('attendance limit', self.session('SX2')['reason'])
        self.assertEqual(self.session('SX3')['state'], 'blocked')

    def test_conflicting_event_id_rejects_whole_batch(self):
        before = report(self.db)
        with self.assertRaises(ValueError):
            self.events('facility-import', [
                {'id': 'ACT1', 'kind': 'activity', 'household_id': 'h1', 'title': 'forged', 'attendees': 99,
                 'starts': '2026-04-03T18:00:00', 'ends': '2026-04-03T20:00:00'},
                {'id': 'SF', 'kind': 'session', 'activity_id': 'ACT2', 'room_id': 'R1',
                 'starts': '2026-04-03T18:00:00', 'ends': '2026-04-03T19:00:00', 'attendees': 4}])
        after = report(self.db)
        self.assertEqual(before, after)
        self.assertIsNone(self.session('SF'))

    def test_file_replay_is_idempotent(self):
        results = self.fixture('facility-import', 'facility-evening.json')
        self.assertEqual(results[0]['result'], 'file_replayed')

    def test_legacy_booking_and_sessions_share_room_exclusion(self):
        for command, name in [('application-import', 'application-ok.json'),
                              ('eligibility-import', 'eligibility-ok.json'),
                              ('allocation-import', 'allocation-ok.json'),
                              ('facility-import', 'facility-ok.json')]:
            self.fixture(command, name)
        self.events('facility-import', [
            {'id': 'ACTF', 'kind': 'activity', 'household_id': 'family-a', 'title': 'visit', 'attendees': 2,
             'starts': '2026-04-03T10:30:00', 'ends': '2026-04-03T11:30:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'ACTG', 'kind': 'activity', 'household_id': 'family-a', 'title': 'talk', 'attendees': 2,
             'starts': '2026-04-03T11:00:00', 'ends': '2026-04-03T12:00:00', 'noise': 'quiet', 'equipment': []},
            {'id': 'SF1', 'kind': 'session', 'activity_id': 'ACTF', 'room_id': 'FR1',
             'starts': '2026-04-03T10:30:00', 'ends': '2026-04-03T11:30:00', 'attendees': 2},
            {'id': 'SF2', 'kind': 'session', 'activity_id': 'ACTG', 'room_id': 'FR1',
             'starts': '2026-04-03T11:00:00', 'ends': '2026-04-03T12:00:00', 'attendees': 2},
            {'id': 'FB2', 'kind': 'facility_booking', 'household_id': 'family-a', 'room_id': 'FR1',
             'starts': '2026-04-03T11:30:00', 'ends': '2026-04-03T12:30:00', 'attendees': 2}])
        self.assertEqual(self.session('SF1')['state'], 'conflict')
        self.assertEqual(self.session('SF2')['state'], 'scheduled')
        self.assertEqual(self.db.execute("SELECT state FROM facility_decision WHERE booking_id='FB2'").fetchone()[0], 'conflict')

    def test_audit_detects_forged_session(self):
        self.fixture('facility-import', 'facility-split.json')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])
        self.db.execute("UPDATE facility_session SET state='forged' WHERE id='SS1'")
        self.db.commit()
        self.assertIn('facility_session', audit(self.db, HANDLERS)['mismatches'])


if __name__ == '__main__':
    unittest.main()
