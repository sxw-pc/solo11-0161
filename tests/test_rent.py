from pathlib import Path
import unittest

from housingqueue.__main__ import import_file, HANDLERS
from housingqueue.store import connect
from housingqueue.report import audit


ROOT = Path(__file__).resolve().parents[1]


class RentCase(unittest.TestCase):
    def test_signed_contract_invoice_receipt_and_audit(self):
        db = connect(':memory:')
        try:
            for command, name in [('application-import', 'application-ok.json'),
                                  ('eligibility-import', 'eligibility-ok.json'),
                                  ('allocation-import', 'allocation-ok.json'),
                                  ('rent-import', 'rent-ok.json')]:
                import_file(db, command, ROOT / 'fixtures' / name)
            row = db.execute("SELECT amount_due,amount_paid,outstanding,state FROM rent_decision WHERE contract_id='R1' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(tuple(row), (1000, 600, 400, 'due'))
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
        finally:
            db.close()
