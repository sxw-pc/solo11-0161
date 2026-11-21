from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
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
