from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
from housingqueue.store import connect


ROOT = Path(__file__).resolve().parents[1]


class ShuttleCase(unittest.TestCase):
    def test_signed_resident_books_vehicle_and_replays(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('shuttle-import', 'shuttle-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            self.assertEqual(db.execute("SELECT state FROM shuttle_decision WHERE request_id='TRIP-1'").fetchone()[0], 'booked')
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()
