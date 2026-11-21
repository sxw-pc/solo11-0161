from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
from housingqueue.store import connect

ROOT = Path(__file__).resolve().parents[1]


class WaterCase(unittest.TestCase):
    def test_tap_sample_and_replay(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            import_file(db, 'water-import', ROOT / 'fixtures' / 'water-ok.json')
            self.assertEqual(db.execute("SELECT state FROM water_sample WHERE id='SAMPLE-U1'").fetchone()[0], 'alert')
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()
