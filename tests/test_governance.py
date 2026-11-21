from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.report import audit
from housingqueue.store import connect


ROOT = Path(__file__).resolve().parents[1]


class GovernanceCase(unittest.TestCase):
    def test_signed_household_votes_and_audit_replays(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('governance-import', 'governance-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            self.assertEqual(db.execute("SELECT state FROM meeting_decision WHERE meeting_id='MEET-1'").fetchone()[0], 'passed')
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()
