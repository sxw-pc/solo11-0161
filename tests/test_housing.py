import json
import tempfile
import unittest
from pathlib import Path
import housingqueue
from housingqueue.store import connect
from housingqueue.__main__ import import_file,HANDLERS
from housingqueue.report import report,audit

ROOT=Path(__file__).resolve().parents[1]

class HousingTest(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.db=connect(Path(self.temp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.temp.cleanup()
    def fixture(self,cmd,name):return import_file(self.db,cmd,ROOT/'fixtures'/name)
    def events(self,cmd,events):
        p=Path(self.temp.name)/'events.json';p.write_text(json.dumps({'events':events}));return import_file(self.db,cmd,p)
    def applied(self):self.fixture('application-import','application-ok.json')
    def qualified(self):self.applied();self.fixture('eligibility-import','eligibility-ok.json')
    def offered(self):
        self.qualified();self.events('allocation-import',[{'id':'U1','kind':'unit','community':'ward-a','rooms':1,'accessible':False,'available_at':'2026-04-01T11:00:00'},
          {'id':'O1','kind':'offer','household_id':'family-a','unit_id':'U1','offered_at':'2026-04-01T12:00:00','expires_at':'2026-04-02T12:00:00'}])
    def test_module_path(self):
        print('module file:',housingqueue.__file__)
        self.assertEqual(Path(housingqueue.__file__).resolve().parents[1],ROOT)
    def test_normal_three_ledgers_and_signed_result(self):
        self.qualified();self.fixture('allocation-import','allocation-ok.json')
        r=report(self.db)
        self.assertEqual(r['application_decision'][-1]['state'],'accepted')
        self.assertEqual(r['application_decision'][-1]['accepted_at'],'2026-04-01T09:30:00')
        self.assertEqual(r['eligibility'][-1]['state'],'qualified')
        self.assertEqual(r['signature'][0]['state'],'signed')
        self.assertEqual(r['household'][0]['status'],'housed')
        self.assertEqual(r['unit'][0]['state'],'assigned')
    def test_member_conflict_freezes_qualification_and_offer(self):
        self.offered();self.fixture('application-import','application-bad.json')
        r=report(self.db)
        self.assertEqual(r['household'][0]['status'],'identity_conflict')
        self.assertEqual(r['eligibility'][-1]['state'],'pending_application')
        self.assertEqual(r['offer'][0]['state'],'frozen')
        self.assertEqual(r['unit'][0]['state'],'available')
    def test_late_property_record_releases_unaccepted_offer(self):
        self.offered();self.fixture('eligibility-import','eligibility-bad.json')
        r=report(self.db)
        self.assertEqual(r['eligibility'][-1]['state'],'ineligible_property')
        self.assertEqual(r['offer'][0]['state'],'frozen')
        self.assertEqual(r['unit'][0]['state'],'available')
    def test_late_property_after_signature_keeps_unit_and_opens_dispute(self):
        self.qualified();self.fixture('allocation-import','allocation-ok.json');self.fixture('eligibility-import','eligibility-bad.json')
        r=report(self.db)
        self.assertEqual(r['offer'][0]['state'],'signed_disputed')
        self.assertEqual(r['unit'][0]['state'],'assigned')
        self.assertEqual(r['household'][0]['status'],'housed_disputed')
        self.assertEqual(r['allocation_dispute'][0]['state'],'open')
    def test_open_objection_freezes_and_resolution_restores(self):
        self.qualified()
        self.events('eligibility-import',[{'id':'OB1','kind':'objection','household_id':'family-a','opened_at':'2026-04-01T11:00:00','closes_at':'2026-04-02T11:00:00','reason':'income discrepancy'}])
        self.assertEqual(report(self.db)['eligibility'][-1]['state'],'frozen_objection')
        self.events('eligibility-import',[{'id':'RO1','kind':'resolve','objection_id':'OB1','at':'2026-04-02T12:00:00','reviewer':'housing-auditor'}])
        self.assertEqual(report(self.db)['eligibility'][-1]['state'],'qualified')
    def test_high_income_denies_qualification(self):
        self.qualified()
        self.events('eligibility-import',[{'id':'I4','kind':'income','household_id':'family-a','month':'2026-03','cents':800000,'issuer':'tax-office','at':'2026-04-01T12:00:00'}])
        self.assertEqual(report(self.db)['eligibility'][-1]['state'],'ineligible_income')
    def test_missing_income_month_is_not_zero(self):
        self.applied()
        self.events('eligibility-import',[{'id':'I1','kind':'income','household_id':'family-a','month':'2026-01','cents':100000,'issuer':'tax-office','at':'2026-04-01T10:00:00'},
          {'id':'P1','kind':'property','household_id':'family-a','area_m2':0,'effective_at':'2026-03-01T00:00:00','issuer':'registry','at':'2026-04-01T10:00:00'}])
        self.assertEqual(report(self.db)['eligibility'][-1]['state'],'pending_income')
    def test_document_revocation_invalidates_qualification(self):
        self.qualified();self.events('application-import',[{'id':'RD1','kind':'revoke_document','document_id':'D3'}])
        r=report(self.db)
        self.assertEqual(r['application_decision'][-1]['state'],'pending')
        self.assertEqual(r['eligibility'][-1]['state'],'pending_application')
    def test_residence_duration_is_not_doc_count(self):
        self.applied()
        self.events('application-import',[{'id':'A4','kind':'household','household_id':'family-a','community':'ward-a','applied_at':'2026-04-01T09:00:00','residence_from':'2025-01-01T00:00:00','version':2}])
        self.assertEqual(report(self.db)['application_decision'][-1]['state'],'ineligible_residence')
    def test_wrong_household_size_rejects_small_unit(self):
        self.qualified()
        self.events('application-import',[{'id':'M4','kind':'member','person_id':'person-c','household_id':'family-a','relation':'parent','joined_at':'2020-01-01T00:00:00'}])
        self.events('allocation-import',[{'id':'U2','kind':'unit','community':'ward-a','rooms':1,'accessible':False,'available_at':'2026-04-01T11:00:00'},
          {'id':'O2','kind':'offer','household_id':'family-a','unit_id':'U2','offered_at':'2026-04-01T12:00:00','expires_at':'2026-04-02T12:00:00'}])
        self.assertEqual(report(self.db)['offer'][0]['state'],'mismatch')
    def test_document_receipt_and_queue_rank_block_out_of_turn_offer(self):
        self.qualified()
        events=[{'id':'AB','kind':'household','household_id':'family-b','community':'ward-a','applied_at':'2026-04-01T08:00:00','residence_from':'2020-01-01T00:00:00','version':1},
          {'id':'MB','kind':'member','person_id':'person-c','household_id':'family-b','relation':'applicant','joined_at':'2020-01-01T00:00:00'},
          {'id':'MC','kind':'member','person_id':'person-d','household_id':'family-b','relation':'child','joined_at':'2020-01-01T00:00:00'}]
        for kind,minute in [('identity','40'),('residence','41'),('relation','42')]:
            events.append({'id':'DB-'+kind,'kind':'document','household_id':'family-b','doc_kind':kind,'subject':'family-b','issued_at':'2026-03-01T00:00:00','received_at':'2026-04-01T09:'+minute+':00','expires_at':'2027-03-01T00:00:00','issuer':'civic-office'})
        self.events('application-import',events)
        self.events('eligibility-import',[{'id':'IB1','kind':'income','household_id':'family-b','month':'2026-01','cents':100000,'issuer':'tax','at':'2026-04-01T10:00:00'},
          {'id':'IB2','kind':'income','household_id':'family-b','month':'2026-02','cents':100000,'issuer':'tax','at':'2026-04-01T10:00:00'},
          {'id':'IB3','kind':'income','household_id':'family-b','month':'2026-03','cents':100000,'issuer':'tax','at':'2026-04-01T10:00:00'},
          {'id':'PB','kind':'property','household_id':'family-b','area_m2':0,'effective_at':'2026-03-01T00:00:00','issuer':'registry','at':'2026-04-01T10:00:00'}])
        positions={q['household_id']:q['rank'] for q in report(self.db)['queue_position'] if q['state']=='waiting'}
        self.assertEqual(positions,{'family-a':1,'family-b':2})
        self.events('allocation-import',[{'id':'U4','kind':'unit','community':'ward-a','rooms':1,'accessible':False,'available_at':'2026-04-01T11:00:00'},
          {'id':'O4','kind':'offer','household_id':'family-b','unit_id':'U4','offered_at':'2026-04-01T12:00:00','expires_at':'2026-04-02T12:00:00'}])
        self.assertEqual(report(self.db)['offer'][0]['state'],'out_of_turn')
    def test_signature_must_be_applicant_and_before_deadline(self):
        self.offered();self.events('allocation-import',[{'id':'S2','kind':'sign','offer_id':'O1','at':'2026-04-01T13:00:00','signer':'person-b'},
          {'id':'S3','kind':'sign','offer_id':'O1','at':'2026-04-03T13:00:00','signer':'person-a'}])
        self.assertEqual([s['state'] for s in report(self.db)['signature']],['invalid','expired'])
        self.assertEqual(report(self.db)['unit'][0]['state'],'held')
    def test_expiry_releases_unit_and_preserves_offer(self):
        self.offered();self.events('allocation-import',[{'id':'EX1','kind':'expire','offer_id':'O1','at':'2026-04-03T12:00:00'}])
        r=report(self.db)
        self.assertEqual(r['offer'][0]['state'],'expired')
        self.assertEqual(r['unit'][0]['state'],'available')
        self.assertEqual(r['queue_position'][-1]['state'],'waiting')
    def test_unqualified_household_cannot_hold_unit(self):
        self.applied();self.events('allocation-import',[{'id':'U3','kind':'unit','community':'ward-a','rooms':1,'accessible':False,'available_at':'2026-04-01T11:00:00'},
          {'id':'O3','kind':'offer','household_id':'family-a','unit_id':'U3','offered_at':'2026-04-01T12:00:00','expires_at':'2026-04-02T12:00:00'}])
        self.assertEqual(report(self.db)['offer'][0]['state'],'blocked')
    def test_raw_file_replay_detects_forged_offer(self):
        self.qualified();self.fixture('allocation-import','allocation-ok.json')
        self.assertTrue(audit(self.db,HANDLERS)['replay_ok'])
        self.db.execute("UPDATE offer SET state='forged' WHERE id='O1'");self.db.commit()
        self.assertEqual(audit(self.db,HANDLERS)['mismatches'],['offer'])
    def test_file_replay_and_conflict(self):
        self.applied();self.assertEqual(self.fixture('application-import','application-ok.json')[0]['result'],'file_replayed')
        with self.assertRaises(ValueError):self.events('application-import',[{'id':'A1','kind':'household','household_id':'family-a','community':'other','applied_at':'2026-04-01T09:00:00','residence_from':'2020-01-01T00:00:00','version':2}])
        self.assertEqual(len(report(self.db)['source_file']),1)

if __name__=='__main__':unittest.main()
