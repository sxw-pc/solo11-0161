from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
from housingqueue.store import connect


ROOT = Path(__file__).resolve().parents[1]


class WasteCase(unittest.TestCase):
    def test_gross_deposit_no_longer_mints_points_without_recyclate(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('waste-import', 'waste-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            # 入站毛重照旧保留
            self.assertEqual(db.execute(
                "SELECT accepted_grams FROM waste_decision WHERE household_id='family-a'").fetchone()[0], 650)
            # 没有实际再生物证据前，毛重不再直接换成积分
            row = db.execute(
                "SELECT points,state,verified_grams FROM reward_decision WHERE household_id='family-a'").fetchone()
            self.assertEqual((row[0], row[1], row[2]), (0, 'pending', 0))
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()
