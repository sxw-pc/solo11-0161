from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
from housingqueue.store import connect


ROOT = Path(__file__).resolve().parents[1]


class WasteCase(unittest.TestCase):
    def test_deposit_creates_points_and_replays(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('waste-import', 'waste-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            self.assertEqual(db.execute("SELECT accepted_grams FROM waste_decision WHERE household_id='family-a'").fetchone()[0], 650)
            self.assertEqual(db.execute("SELECT points FROM reward_decision WHERE household_id='family-a'").fetchone()[0], 6)
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()
