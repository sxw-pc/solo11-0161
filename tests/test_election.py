"""Acceptance tests for the versioned community election charter."""
import json
import tempfile
import unittest
from pathlib import Path

from housingqueue.__main__ import HANDLERS, import_file
from housingqueue.report import audit, report
from housingqueue.store import connect, many, one

ROOT = Path(__file__).resolve().parents[1]


class ElectionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'test.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def fixture(self, cmd, name):
        return import_file(self.db, cmd, ROOT / 'fixtures' / name)

    def events(self, cmd, events):
        path = Path(self.temp.name) / 'events.json'
        path.write_text(json.dumps({'events': events}))
        return import_file(self.db, cmd, path)

    def nine(self, charter='governance-nine-ir.json'):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-allocation.json')
        self.fixture('governance-import', charter)

    def decision(self, version=None):
        if version is None:
            return one(self.db, 'SELECT * FROM decision_version ORDER BY version DESC LIMIT 1')
        return one(self.db, 'SELECT * FROM decision_version WHERE version=?', (version,))

    def rounds(self, version=None):
        version = version or self.decision()['version']
        return many(self.db, 'SELECT * FROM decision_round WHERE version=? ORDER BY round_no', (version,))

    # ------------------------------------------------------------------
    # the two chartered routes over the same nine ballots
    # ------------------------------------------------------------------
    def test_plurality_charter_wins_with_four_first_preferences(self):
        self.nine('governance-nine-plurality.json')
        decision = self.decision()
        self.assertEqual((decision['method'], decision['winner'], decision['outcome']),
                         ('plurality', 'X', 'winner'))
        self.assertEqual(decision['attendance'], 9)
        self.assertEqual(decision['quorum'], 7)
        self.assertEqual(json.loads(self.rounds()[0]['totals']), {'X': 4, 'Y': 3, 'Z': 2})
        evidence = json.loads(decision['evidence'])
        self.assertEqual(evidence['charter']['method'], 'plurality')
        self.assertEqual(evidence['charter']['id'], 'CH-PLURALITY')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_ir_charter_transfers_eliminated_preferences_to_y(self):
        self.nine('governance-nine-ir.json')
        decision = self.decision()
        self.assertEqual((decision['method'], decision['winner']), ('ir_elimination', 'Y'))
        rounds = self.rounds()
        self.assertEqual(len(rounds), 2)
        first, second = rounds
        self.assertEqual(json.loads(first['totals']), {'X': 4, 'Y': 3, 'Z': 2})
        self.assertEqual(first['eliminated'], 'Z')
        self.assertEqual(json.loads(first['transfers']), {'Y': 2})
        self.assertEqual(json.loads(second['totals']), {'X': 4, 'Y': 5})
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_both_routes_are_chartered_and_fixed_before_the_count(self):
        self.nine('governance-nine-plurality.json')
        evidence = json.loads(self.decision()['evidence'])
        self.assertEqual(evidence['charter']['tie_break'], 'candidate_order')
        self.assertEqual(evidence['charter']['quorum'], 7)
        self.assertEqual(evidence['charter']['roster_deadline'], '2026-05-01T18:00:00')
        self.assertEqual(len(evidence['roster']), 9)
        # a meeting cannot swap its method after ballots exist
        with self.assertRaises(ValueError):
            self.events('governance-import', [{'id': 'CH-OTHER', 'kind': 'charter',
                                               'meeting_id': 'MEET-9', 'contest': ['X', 'Y', 'Z'],
                                               'roster_deadline': '2026-05-01T18:00:00',
                                               'quorum': 7, 'method': 'ir_elimination',
                                               'tie_break': 'candidate_order',
                                               'published_at': '2026-05-02T09:00:00'}])

    def test_announced_version_cannot_be_rewritten(self):
        self.nine('governance-nine-ir.json')
        with self.assertRaises(Exception):
            self.db.execute("UPDATE decision_version SET winner='X'")

    # ------------------------------------------------------------------
    # roster / allocation signing time and application identity
    # ------------------------------------------------------------------
    def test_late_signature_is_excluded_and_tie_goes_to_candidate_order(self):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-late-sign-allocation.json')
        self.fixture('governance-import', 'governance-nine-late-sign.json')
        decision = self.decision()
        self.assertEqual((decision['winner'], decision['outcome'], decision['attendance']),
                         ('X', 'winner', 8))
        self.assertEqual(json.loads(self.rounds()[0]['totals']), {'X': 4, 'Y': 3, 'Z': 1})
        self.assertEqual(json.loads(self.rounds()[1]['totals']), {'X': 4, 'Y': 4})
        ballot = one(self.db, "SELECT * FROM decision_ballot WHERE ballot_id='G-V9'")
        self.assertEqual((ballot['status'], ballot['reason']),
                         ('disputed', 'signed_after_roster_deadline'))
        # the other eight ballots survive untouched
        counted = many(self.db, "SELECT ballot_id FROM decision_ballot WHERE status='counted' ORDER BY ballot_id")
        self.assertEqual([r['ballot_id'] for r in counted], [f'G-V{i}' for i in range(1, 9)])
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_current_applicant_identity_is_not_backdated(self):
        self._base_with_charter()
        # a replacement applicant joins only after the roster deadline and then
        # signs; the current-state lookup would accept them, the deadline
        # effective-applicant lookup must not.
        self.events('application-import', [
            {'id': 'G-LATE-APP', 'kind': 'member', 'person_id': 'person-late',
             'household_id': 'family-01', 'relation': 'applicant',
             'joined_at': '2026-05-04T09:00:00'}])
        self.events('governance-import', [
            {'id': 'B1', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
             'at': '2026-05-05T10:00:00', 'ranked': ['X'], 'signer': 'person-late'},
            {'id': 'AN', 'kind': 'announce', 'meeting_id': 'M', 'at': '2026-05-07T20:00:00'}])
        ballot = one(self.db, "SELECT * FROM decision_ballot WHERE ballot_id='B1'")
        self.assertEqual(ballot['status'], 'disputed')
        self.assertEqual(ballot['reason'], 'unauthorized_signer')
        # the effective applicant at the deadline remains the original one
        self.assertEqual(
            one(self.db, "SELECT person_id FROM roster_snapshot WHERE household_id='family-01'")['person_id'],
            'person-01')

    # ------------------------------------------------------------------
    # proxies: duplicate self+agent, cycles, late/foreign authorisations
    # ------------------------------------------------------------------
    def _base_with_charter(self, method='plurality'):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-allocation.json')
        self.events('governance-import', [
            {'id': 'M', 'kind': 'meeting', 'community': 'ward-a',
             'opens': '2026-05-01T00:00:00', 'closes': '2026-05-08T00:00:00', 'title': 'e'},
            {'id': 'C', 'kind': 'charter', 'meeting_id': 'M', 'contest': ['X', 'Y', 'Z'],
             'roster_deadline': '2026-05-01T18:00:00', 'quorum': 7, 'method': method,
             'tie_break': 'candidate_order', 'published_at': '2026-04-20T09:00:00'}])

    def test_self_and_proxy_cannot_both_count(self):
        self._base_with_charter()
        self.events('governance-import', [
            {'id': 'AUTH1', 'kind': 'authorize', 'meeting_id': 'M', 'household_id': 'family-01',
             'authorizer': 'person-01', 'agent': 'person-02', 'at': '2026-04-25T09:00:00',
             'signature': 'written-1'},
            {'id': 'B1', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
             'at': '2026-05-02T10:00:00', 'ranked': ['X'], 'signer': 'person-01'},
            {'id': 'B2', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
             'at': '2026-05-02T11:00:00', 'ranked': ['Y'], 'signer': 'person-02'},
            {'id': 'AN', 'kind': 'announce', 'meeting_id': 'M', 'at': '2026-05-07T20:00:00'}])
        rows = {r['ballot_id']: r for r in many(self.db, 'SELECT * FROM decision_ballot')}
        self.assertEqual((rows['B1']['status'], rows['B1']['counted']), ('counted', 1))
        self.assertEqual(rows['B2']['status'], 'duplicate_superseded')
        self.assertEqual(rows['B2']['counted'], 0)

    def test_two_proxy_ballots_for_one_household_are_isolated(self):
        self._base_with_charter()
        self.events('governance-import', [
            {'id': 'A1', 'kind': 'authorize', 'meeting_id': 'M', 'household_id': 'family-01',
             'authorizer': 'person-01', 'agent': 'person-02', 'at': '2026-04-25T09:00:00',
             'signature': 'w1'},
            {'id': 'A2', 'kind': 'authorize', 'meeting_id': 'M', 'household_id': 'family-01',
             'authorizer': 'person-01', 'agent': 'person-03', 'at': '2026-04-25T10:00:00',
             'signature': 'w2'},
            {'id': 'P1', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
             'at': '2026-05-02T10:00:00', 'ranked': ['X'], 'signer': 'person-02'},
            {'id': 'P2', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
             'at': '2026-05-02T11:00:00', 'ranked': ['Y'], 'signer': 'person-03'},
            {'id': 'AN', 'kind': 'announce', 'meeting_id': 'M', 'at': '2026-05-07T20:00:00'}])
        rows = {r['ballot_id']: r for r in many(self.db, 'SELECT * FROM decision_ballot')}
        self.assertEqual(rows['P1']['status'], 'disputed')
        self.assertEqual(rows['P2']['status'], 'disputed')
        self.assertEqual(rows['P1']['reason'], 'duplicate_agent_ballot')
        self.assertNotEqual(rows['P1']['status'], 'counted')
        self.assertNotEqual(rows['P2']['status'], 'counted')

    def test_proxy_chain_cycle_is_isolated(self):
        self._base_with_charter()
        self.events('governance-import', [
            {'id': 'A1', 'kind': 'authorize', 'meeting_id': 'M', 'household_id': 'family-01',
             'authorizer': 'person-01', 'agent': 'person-02', 'at': '2026-04-25T09:00:00',
             'signature': 'w1'},
            {'id': 'A2', 'kind': 'authorize', 'meeting_id': 'M', 'household_id': 'family-02',
             'authorizer': 'person-02', 'agent': 'person-01', 'at': '2026-04-25T10:00:00',
             'signature': 'w2'},
            {'id': 'P1', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
             'at': '2026-05-02T10:00:00', 'ranked': ['X'], 'signer': 'person-02'},
            {'id': 'AN', 'kind': 'announce', 'meeting_id': 'M', 'at': '2026-05-07T20:00:00'}])
        row = one(self.db, "SELECT * FROM decision_ballot WHERE ballot_id='P1'")
        self.assertEqual((row['status'], row['reason']), ('disputed', 'agent_cycle'))

    def test_late_proxy_authorisation_is_isolated(self):
        self._base_with_charter()
        self.events('governance-import', [
            {'id': 'A1', 'kind': 'authorize', 'meeting_id': 'M', 'household_id': 'family-01',
             'authorizer': 'person-01', 'agent': 'person-02', 'at': '2026-05-03T09:00:00',
             'signature': 'w1'},
            {'id': 'P1', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
             'at': '2026-05-04T10:00:00', 'ranked': ['X'], 'signer': 'person-02'},
            {'id': 'AN', 'kind': 'announce', 'meeting_id': 'M', 'at': '2026-05-07T20:00:00'}])
        row = one(self.db, "SELECT * FROM decision_ballot WHERE ballot_id='P1'")
        self.assertEqual((row['status'], row['reason']), ('disputed', 'authorization_expired'))

    # ------------------------------------------------------------------
    # quorum
    # ------------------------------------------------------------------
    def test_quorum_shortfall_declares_no_winner(self):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-allocation.json')
        events = [{'id': 'M', 'kind': 'meeting', 'community': 'ward-a',
                   'opens': '2026-05-01T00:00:00', 'closes': '2026-05-08T00:00:00', 'title': 'e'},
                  {'id': 'C', 'kind': 'charter', 'meeting_id': 'M', 'contest': ['X', 'Y', 'Z'],
                   'roster_deadline': '2026-05-01T18:00:00', 'quorum': 7, 'method': 'plurality',
                   'tie_break': 'candidate_order', 'published_at': '2026-04-20T09:00:00'}]
        for i in range(1, 7):
            events.append({'id': f'Q{i}', 'kind': 'ballot', 'meeting_id': 'M',
                           'household_id': f'family-{i:02d}',
                           'at': f'2026-05-02T10:0{i}:00', 'ranked': ['X'],
                           'signer': f'person-{i:02d}'})
        events.append({'id': 'AN', 'kind': 'announce', 'meeting_id': 'M', 'at': '2026-05-07T20:00:00'})
        self.events('governance-import', events)
        decision = self.decision()
        self.assertEqual((decision['outcome'], decision['winner'], decision['attendance']),
                         ('no_quorum', None, 6))

    def test_abstention_counts_for_attendance_not_preferences(self):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-allocation.json')
        events = [{'id': 'M', 'kind': 'meeting', 'community': 'ward-a',
                   'opens': '2026-05-01T00:00:00', 'closes': '2026-05-08T00:00:00', 'title': 'e'},
                  {'id': 'C', 'kind': 'charter', 'meeting_id': 'M', 'contest': ['X', 'Y', 'Z'],
                   'roster_deadline': '2026-05-01T18:00:00', 'quorum': 3, 'method': 'plurality',
                   'tie_break': 'candidate_order', 'published_at': '2026-04-20T09:00:00'}]
        for i in range(1, 3):
            events.append({'id': f'Q{i}', 'kind': 'ballot', 'meeting_id': 'M',
                           'household_id': f'family-{i:02d}',
                           'at': f'2026-05-02T10:0{i}:00', 'ranked': ['X'],
                           'signer': f'person-{i:02d}'})
        events.append({'id': 'Q3', 'kind': 'ballot', 'meeting_id': 'M',
                       'household_id': 'family-03', 'at': '2026-05-02T10:03:00',
                       'ranked': [], 'signer': 'person-03'})
        events.append({'id': 'AN', 'kind': 'announce', 'meeting_id': 'M', 'at': '2026-05-07T20:00:00'})
        self.events('governance-import', events)
        decision = self.decision()
        self.assertEqual((decision['attendance'], decision['winner'], decision['outcome']),
                         (3, 'X', 'winner'))
        self.assertEqual(json.loads(self.rounds()[0]['totals']), {'X': 2, 'Y': 0, 'Z': 0})

    # ------------------------------------------------------------------
    # corrections / withdrawal re-count as a new version only
    # ------------------------------------------------------------------
    def _announce_nine(self, charter='governance-nine-ir.json'):
        self.nine(charter)

    def test_correction_after_announcement_appends_a_version(self):
        self._announce_nine()
        before = self.decision()
        self.assertEqual(before['winner'], 'Y')
        self.events('governance-import', [
            {'id': 'COR1', 'kind': 'correction', 'ballot_id': 'G-V1',
             'ranked': ['Z', 'Y', 'X'], 'at': '2026-05-07T21:00:00', 'signer': 'person-01'},
            {'id': 'AN2', 'kind': 'announce', 'meeting_id': 'MEET-9', 'at': '2026-05-07T22:00:00'}])
        versions = many(self.db, 'SELECT * FROM decision_version ORDER BY version')
        self.assertEqual(len(versions), 2)
        old, new = versions
        self.assertEqual((old['version'], old['winner']), (1, 'Y'))
        self.assertEqual(new['supersedes'], 1)
        self.assertNotEqual((new['winner'], json.loads(new['evidence'])['rounds']),
                            (old['winner'], json.loads(old['evidence'])['rounds']))
        delta = many(self.db, "SELECT * FROM decision_delta WHERE version=?", (new['version'],))
        self.assertEqual({d['ballot_id'] for d in delta}, {'G-V1'})
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_withdrawal_after_announcement_appends_a_version(self):
        self._announce_nine('governance-nine-plurality.json')
        self.assertEqual(self.decision()['winner'], 'X')
        self.events('governance-import', [
            {'id': 'W1', 'kind': 'withdrawal', 'meeting_id': 'MEET-9', 'candidate': 'X',
             'at': '2026-05-07T21:00:00'},
            {'id': 'AN2', 'kind': 'announce', 'meeting_id': 'MEET-9', 'at': '2026-05-07T22:00:00'}])
        old, new = many(self.db, 'SELECT * FROM decision_version ORDER BY version')
        self.assertEqual(old['winner'], 'X')
        self.assertEqual(new['version'], 2)
        self.assertEqual(new['winner'], 'Y')
        self.assertEqual(json.loads(new['evidence'])['withdrawn'], ['X'])
        self.assertTrue(json.loads(new['evidence'])['winner_changed'])

    def test_identical_recount_is_not_duplicated(self):
        self._announce_nine('governance-nine-plurality.json')
        result = self.events('governance-import', [
            {'id': 'AN2', 'kind': 'announce', 'meeting_id': 'MEET-9', 'at': '2026-05-07T21:00:00'}])
        self.assertEqual(result[0]['result'], 'unchanged')
        self.assertEqual(len(many(self.db, 'SELECT * FROM decision_version')), 1)

    def test_late_arriving_signature_evidence_appends_a_version(self):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-pending-sign-allocation.json')
        self.fixture('governance-import', 'governance-nine-pending-sign.json')
        first = self.decision()
        self.assertEqual((first['version'], first['winner'], first['attendance']), (1, 'X', 8))
        held = one(self.db, "SELECT * FROM decision_ballot WHERE ballot_id='G-V9'")
        self.assertEqual((held['status'], held['reason']), ('disputed', 'no_signature_evidence'))
        # evidence arrives later but records a signing time before the deadline
        self.fixture('allocation-import', 'governance-nine-sign-evidence.json')
        self.fixture('governance-import', 'governance-nine-reannounce.json')
        old, new = many(self.db, 'SELECT * FROM decision_version ORDER BY version')
        self.assertEqual((old['winner'], old['attendance']), ('X', 8))
        self.assertEqual((new['version'], new['winner'], new['attendance'], new['supersedes']), (2, 'Y', 9, 1))
        evidence = json.loads(new['evidence'])
        self.assertEqual(evidence['changed_ballots'], ['G-V9'])
        self.assertTrue(evidence['winner_changed'])
        self.assertEqual(evidence['previous_winner'], 'X')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    # ------------------------------------------------------------------
    # legacy path and immutability
    # ------------------------------------------------------------------
    def test_proxy_duplicate_fixture_counts_one_vote_per_household(self):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-allocation.json')
        self.fixture('governance-import', 'governance-proxy-duplicate.json')
        rows = {r['ballot_id']: r for r in many(self.db, 'SELECT * FROM decision_ballot')}
        self.assertEqual((rows['P-B1']['status'], rows['P-B1']['counted']), ('counted', 1))
        self.assertEqual(rows['P-B2']['status'], 'duplicate_superseded')
        self.assertEqual(self.decision()['attendance'], 3)
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_proxy_cycle_fixture_isolates_the_looped_ballot(self):
        self.fixture('application-import', 'governance-nine-application.json')
        self.fixture('eligibility-import', 'governance-nine-eligibility.json')
        self.fixture('allocation-import', 'governance-nine-allocation.json')
        self.fixture('governance-import', 'governance-proxy-cycle.json')
        row = one(self.db, "SELECT * FROM decision_ballot WHERE ballot_id='C-B1'")
        self.assertEqual((row['status'], row['reason']), ('disputed', 'agent_cycle'))
        self.assertEqual(self.decision()['outcome'], 'no_quorum')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_withdrawal_fixture_records_new_version(self):
        self.nine('governance-nine-plurality.json')
        self.fixture('governance-import', 'governance-nine-withdrawal.json')
        old, new = many(self.db, 'SELECT * FROM decision_version ORDER BY version')
        self.assertEqual((old['winner'], new['winner'], new['supersedes']), ('X', 'Y', 1))
        self.assertEqual(json.loads(new['evidence'])['withdrawn'], ['X'])
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_correction_fixture_lists_the_changed_ballot(self):
        self.nine('governance-nine-ir.json')
        self.fixture('governance-import', 'governance-nine-correction.json')
        new = self.decision()
        self.assertEqual(json.loads(new['evidence'])['changed_ballots'], ['G-V5'])
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_original_governance_import_still_works(self):
        for command, name in [('application-import', 'application-ok.json'),
                              ('eligibility-import', 'eligibility-ok.json'),
                              ('allocation-import', 'allocation-ok.json'),
                              ('governance-import', 'governance-ok.json')]:
            self.fixture(command, name)
        decision = one(self.db, "SELECT * FROM meeting_decision WHERE meeting_id='MEET-1'")
        self.assertEqual((decision['yes_votes'], decision['state']), (1, 'passed'))
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_event_id_conflict_across_content_is_rejected(self):
        self._base_with_charter()
        ballot = {'id': 'B1', 'kind': 'ballot', 'meeting_id': 'M', 'household_id': 'family-01',
                  'at': '2026-05-02T10:00:00', 'ranked': ['X'], 'signer': 'person-01'}
        self.events('governance-import', [ballot])
        changed = dict(ballot, ranked=['Y'])
        with self.assertRaises(ValueError):
            self.events('governance-import', [changed])

    def test_tampered_source_file_breaks_audit(self):
        self.nine('governance-nine-ir.json')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])
        self.db.execute("UPDATE decision_round SET totals=json_set(totals,'$.X',99)")
        self.db.commit()
        self.assertEqual(audit(self.db, HANDLERS)['mismatches'], ['decision_round'])


if __name__ == '__main__':
    unittest.main()
