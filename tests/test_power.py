from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
from housingqueue.store import connect


ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'fixtures'


class PowerCase(unittest.TestCase):
    def test_signed_unit_meter_enters_community_demand_after_delivery(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('dispatch-import', 'dispatch-deliver-u1.json'),
                                  ('power-import', 'power-ok.json')]:
                import_file(db, command, F / name)
            # dispatch-setup delivers U1, so its latest reading is counted
            row = db.execute("SELECT total_kw_tenths,reading_count FROM power_demand WHERE community='ward-a'").fetchone()
            self.assertEqual(tuple(row), (25, 1))
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()

    def test_signed_but_undelivered_unit_reading_is_pending_not_zero(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('power-import', 'power-ok.json')]:
                import_file(db, command, F / name)
            # signed only: no physical delivery, so no occupied load and no implicit zero
            row = db.execute("SELECT total_kw_tenths,reading_count,state FROM power_demand WHERE community='ward-a'").fetchone()
            self.assertEqual(tuple(row), (0, 0, 'pending'))
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
