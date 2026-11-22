import json
import tempfile
import unittest
from pathlib import Path
from housingqueue.store import connect
from housingqueue.__main__ import import_file,HANDLERS
from housingqueue.report import report,audit

ROOT=Path(__file__).resolve().parents[1]

class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.temp.name)/'test.sqlite')
    def tearDown(self):
        self.db.close();self.temp.cleanup()
    def fixture(self,cmd,name):return import_file(self.db,cmd,ROOT/'fixtures'/name)
    def events(self,cmd,events):
        p=Path(self.temp.name)/'events.json';p.write_text(json.dumps({'events':events}))
        return import_file(self.db,cmd,p)
    def base(self):
        self.fixture('application-import','maintenance-application.json')
        self.fixture('eligibility-import','maintenance-eligibility.json')
        self.fixture('allocation-import','maintenance-allocation.json')
    def by_id(self,table,key,id_):
        rows=[x for x in report(self.db)[table] if x[key]==id_]
        return rows[-1] if rows else None
    def state(self,table,key,id_,field='state'):
        x=self.by_id(table,key,id_);return None if x is None else x[field]

    def test_path_a_repair_reoccupy_and_h2_offer(self):
        self.base()
        self.fixture('maintenance-import','maintenance-path-a-repair.json')
        self.fixture('allocation-import','maintenance-path-a-h2-offer.json')
        r=report(self.db)
        # U1 outage window is rebuilt from events; it is back to assigned, never pooled
        self.assertEqual(self.state('unit','id','U1'),'assigned')
        self.assertEqual(self.state('decommission','id','DC1'),'restored')
        self.assertEqual(self.state('work_order','id','WO1'),'verified')
        self.assertEqual(self.state('reinspection','work_order_id','WO1'),'passed')
        # swing stay consumed real capacity then released it; original lease untouched
        self.assertEqual(self.state('temporary_placement','id','TP1'),'ended')
        self.assertEqual(self.state('unit','id','U3'),'available')
        self.assertEqual(self.by_id('offer','id','MO1')['state'],'signed')
        # H2 receives a real offer on U2
        self.assertEqual(self.by_id('offer','id','MO2')['state'],'offered')
        self.assertEqual(self.state('unit','id','U2'),'held')
        self.assertTrue(audit(self.db,HANDLERS)['replay_ok'])

    def test_path_b_permanent_replacement_keeps_defective_unit_out_of_pool(self):
        self.base()
        self.fixture('maintenance-import','maintenance-path-b-replacement.json')
        r=report(self.db)
        self.assertEqual(self.state('unit','id','U1'),'decommissioned')
        self.assertEqual(self.state('unit','id','U2'),'assigned')
        self.assertEqual(self.state('unit','id','U3'),'available')
        self.assertEqual(self.state('permanent_replacement','id','PR1'),'completed')
        self.assertEqual(self.state('temporary_placement','id','TP1'),'ended')
        # original signed lease evidence retained; no reallocation of U1
        self.assertEqual(self.by_id('offer','id','MO1')['state'],'signed')
        # H2 never received a U2 offer on this path
        self.assertFalse(any(o['unit_id']=='U2' for o in r['offer']))
        self.assertTrue(audit(self.db,HANDLERS)['replay_ok'])

    def test_same_input_choice_changes_h2_offer_and_h1_destination(self):
        # both paths share the identical base ledger; only the maintenance branch differs
        self.base()
        self.fixture('maintenance-import','maintenance-path-a-repair.json')
        self.fixture('allocation-import','maintenance-path-a-h2-offer.json')
        a=report(self.db)
        db2=connect(Path(self.temp.name)/'b.sqlite')
        self.db.close();self.db=db2
        self.base()
        self.fixture('maintenance-import','maintenance-path-b-replacement.json')
        b=report(self.db)
        self.assertEqual([o['state'] for o in a['offer'] if o['id']=='MO2'],['offered'])
        self.assertFalse(any(o['id']=='MO2' for o in b['offer']))
        self.assertTrue(any(x['state']=='completed' for x in a['reoccupation']))
        self.assertEqual([p['state'] for p in b['permanent_replacement']],['completed'])

    def test_reinspection_conflict_keeps_outage_and_accountability(self):
        self.base()
        self.fixture('maintenance-import','maintenance-conflict.json')
        r=report(self.db)
        # crew self-reported completion but independent evidence failed
        self.assertEqual(self.state('work_order','id','WO1'),'disputed')
        self.assertEqual(self.state('reinspection','work_order_id','WO1'),'failed')
        self.assertEqual(len(r['accountability']),1)
        self.assertEqual(self.state('unit','id','U1'),'decommissioned')
        # temporary placement is not auto-ended by the work-order completion flag
        self.assertEqual(self.state('temporary_placement','id','TP1'),'active')
        self.assertEqual(self.state('unit','id','U3'),'held')
        with self.assertRaises(ValueError):
            self.events('maintenance-import',[{'id':'X1','kind':'return_to_service','unit_id':'U1','at':'2026-04-16T09:00:00'}])
        with self.assertRaises(ValueError):
            self.events('maintenance-import',[{'id':'X2','kind':'reoccupy','placement_id':'TP1','at':'2026-04-16T09:00:00'}])

    def test_no_offer_during_outage_or_into_swing_unit(self):
        self.base()
        self.fixture('maintenance-import','maintenance-conflict.json')
        r1=self.events('allocation-import',[{'id':'B1','kind':'offer','household_id':'family-b','unit_id':'U1','offered_at':'2026-04-16T12:00:00','expires_at':'2026-04-17T12:00:00'}])
        r2=self.events('allocation-import',[{'id':'B2','kind':'offer','household_id':'family-b','unit_id':'U3','offered_at':'2026-04-16T12:00:00','expires_at':'2026-04-17T12:00:00'}])
        self.assertEqual(r1[0]['result'],'blocked')
        self.assertEqual(r2[0]['result'],'blocked')
        self.assertEqual(self.state('unit','id','U1'),'decommissioned')

    def test_late_defect_freezes_unsigned_offer_and_restores_queue_position(self):
        self.base()
        self.fixture('application-import','maintenance-late-application.json')
        self.fixture('eligibility-import','maintenance-late-eligibility.json')
        self.fixture('allocation-import','maintenance-late-defect-setup.json')
        self.assertEqual(self.by_id('offer','id','LO1')['state'],'offered')
        self.fixture('maintenance-import','maintenance-late-defect.json')
        r=report(self.db)
        self.assertEqual(self.by_id('offer','id','LO1')['state'],'frozen')
        self.assertEqual(self.state('unit','id','U4'),'decommissioned')
        # household-c gets the waiting position back rather than a new application
        last=[q for q in r['queue_position'] if q['household_id']=='family-c'][-1]
        self.assertEqual(last['state'],'waiting')
        self.assertTrue(any('next eligible household checked' in x['reason'] for x in r['decision_log']))

    def test_late_defect_before_signed_move_in_opens_safety_dispute(self):
        self.base()
        self.fixture('application-import','maintenance-late-application.json')
        self.fixture('eligibility-import','maintenance-late-eligibility.json')
        self.fixture('allocation-import','maintenance-late-defect-setup.json')
        self.fixture('maintenance-import','maintenance-late-defect.json')
        r=report(self.db)
        # signed lease preserved, unit never returned to the allocatable pool
        self.assertEqual(self.by_id('offer','id','MO1')['state'],'signed')
        self.assertEqual(self.state('unit','id','U1'),'decommissioned')
        dispute=r['safety_dispute'][0]
        self.assertEqual(dispute['offer_id'],'MO1');self.assertEqual(dispute['state'],'open')
        self.assertEqual(self.by_id('household','id','family-a')['status'],'housed_disputed')
        # placement duty still owed and delivered via swing capacity
        self.assertEqual(self.state('temporary_placement','id','LTP1'),'active')
        self.assertEqual(self.state('unit','id','U3'),'held')

    def _pledged_before_handover(self):
        self.base()
        ev=json.loads((ROOT/'fixtures'/'maintenance-path-b-replacement.json').read_text())['events']
        p=Path(self.temp.name)/'branch.json'
        for e in ev[:-1]:
            p.write_text(json.dumps({'events':[e]}));import_file(self.db,'maintenance-import',p)
        return p

    def test_late_ineligibility_freezes_replacement_but_keeps_placement_duty(self):
        p=self._pledged_before_handover()
        self.fixture('eligibility-import','maintenance-late-ineligible.json')
        r=report(self.db)
        self.assertEqual(self.state('permanent_replacement','id','PR1'),'frozen')
        self.assertEqual(self.state('unit','id','U2'),'available')
        # temporary safety placement survives the later eligibility version
        self.assertEqual(self.state('temporary_placement','id','TP1'),'active')
        self.assertEqual(self.state('unit','id','U3'),'held')
        # U2 returns to the queue: H2 gets a real offer opportunity
        p.write_text(json.dumps({'events':[{'id':'MO9','kind':'offer','household_id':'family-b','unit_id':'U2','offered_at':'2026-04-17T12:00:00','expires_at':'2026-04-18T12:00:00'}]}))
        self.assertEqual(import_file(self.db,'allocation-import',p)[0]['result'],'offered')
        adj=[a for a in r['placement_adjudication'] if a['household_id']=='family-a'][0]
        self.assertEqual(adj['original_lease_eligibility']['state'],'qualified')
        self.assertEqual(adj['current_replacement_eligibility']['state'],'ineligible_income')
        self.assertEqual(adj['temporary_safety_placement_duty']['state'],'duty_active')

    def test_requalification_reconfirms_pledge_when_unit_not_signed_away(self):
        p=self._pledged_before_handover()
        self.fixture('eligibility-import','maintenance-late-ineligible.json')
        p.write_text(json.dumps({'events':[{'id':'MO9','kind':'offer','household_id':'family-b','unit_id':'U2','offered_at':'2026-04-17T12:00:00','expires_at':'2026-04-18T12:00:00'}]}))
        import_file(self.db,'allocation-import',p)
        p.write_text(json.dumps({'events':[{'id':'HII5','kind':'income','household_id':'family-a','month':'2026-03','cents':120000,'issuer':'tax-office','at':'2026-04-17T15:00:00'}]}))
        import_file(self.db,'eligibility-import',p)
        self.assertEqual(self.state('permanent_replacement','id','PR1'),'pledged')
        self.assertEqual(self.by_id('offer','id','MO9')['state'],'frozen')
        self.assertEqual(self.state('unit','id','U2'),'held')
        self.assertTrue(audit(self.db,HANDLERS)['replay_ok'])

    def test_requalification_does_not_revive_pledge_once_unit_signed_away(self):
        p=self._pledged_before_handover()
        self.fixture('eligibility-import','maintenance-late-ineligible.json')
        p.write_text(json.dumps({'events':[{'id':'MO9','kind':'offer','household_id':'family-b','unit_id':'U2','offered_at':'2026-04-17T12:00:00','expires_at':'2026-04-18T12:00:00'}]}))
        import_file(self.db,'allocation-import',p)
        p.write_text(json.dumps({'events':[{'id':'MS9','kind':'sign','offer_id':'MO9','at':'2026-04-17T13:00:00','signer':'person-b1'}]}))
        import_file(self.db,'allocation-import',p)
        p.write_text(json.dumps({'events':[{'id':'HII5','kind':'income','household_id':'family-a','month':'2026-03','cents':120000,'issuer':'tax-office','at':'2026-04-17T15:00:00'}]}))
        import_file(self.db,'eligibility-import',p)
        self.assertEqual(self.state('permanent_replacement','id','PR1'),'frozen')
        self.assertEqual(self.by_id('offer','id','MO9')['state'],'signed')
        self.assertEqual(self.state('unit','id','U2'),'assigned')

    def test_event_id_idempotency_content_conflict_and_full_replay(self):
        self.base()
        self.fixture('maintenance-import','maintenance-path-a-repair.json')
        # replaying the identical file is a no-op
        again=self.fixture('maintenance-import','maintenance-path-a-repair.json')
        self.assertTrue(all(x['result']=='event_replayed' or x['result']=='file_replayed' for x in again))
        # same event id with different content is rejected
        with self.assertRaises(ValueError):
            self.events('maintenance-import',[{'id':'DF1','kind':'defect_report','unit_id':'U1','reported_at':'2026-04-08T09:00:00','reporter':'family-a','summary':'CHANGED CONTENT'}])
        self.assertTrue(audit(self.db,HANDLERS)['replay_ok'])

    def test_report_separates_three_obligations(self):
        self.base()
        self.fixture('maintenance-import','maintenance-conflict.json')
        adj=[a for a in report(self.db)['placement_adjudication'] if a['household_id']=='family-a'][0]
        self.assertEqual(adj['original_lease_eligibility']['unit_id'],'U1')
        self.assertEqual(adj['original_lease_eligibility']['state'],'qualified')
        self.assertEqual(adj['current_replacement_eligibility']['state'],'qualified')
        self.assertIsNone(adj['current_replacement_eligibility']['pledge_state'])
        self.assertEqual(adj['temporary_safety_placement_duty']['state'],'duty_active')
        self.assertEqual(adj['temporary_safety_placement_duty']['swing_unit_id'],'U3')
        # it is a derived view, not the flattened household.status
        self.assertEqual(self.by_id('household','id','family-a')['status'],'housed')

if __name__=='__main__':unittest.main()
