from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
from housingqueue.store import connect


ROOT = Path(__file__).resolve().parents[1]


class PowerCase(unittest.TestCase):
    def test_signed_unit_meter_enters_community_demand(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('power-import', 'power-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            row = db.execute("SELECT total_kw_tenths,reading_count FROM power_demand WHERE community='ward-a'").fetchone()
            self.assertEqual(tuple(row), (25, 1))
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()
