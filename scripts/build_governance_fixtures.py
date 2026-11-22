"""Generate governance acceptance fixtures.

Each household in this fixture series signs its allocation in April 2026 and
the community meeting opens in May 2026.  Households are synthetic.  Run with
``python3 scripts/build_governance_fixtures.py`` from the repository root.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIXTURES = HERE / 'fixtures'

SIGN_AT = '2026-04-10T09:00:00'
MEETING = {'id': 'MEET-9', 'kind': 'meeting', 'community': 'ward-a',
           'opens': '2026-05-01T00:00:00', 'closes': '2026-05-08T00:00:00',
           'title': 'community representative election'}
DEADLINE = '2026-05-01T18:00:00'
PUBLISHED = '2026-04-20T09:00:00'
ANNOUNCE_AT = '2026-05-07T20:00:00'

# Nine households.  Four X>Y>Z, three Y>Z>X, two Z>Y>X.
RANKING = (
    ['X>Y>Z'] * 4 +
    ['Y>Z>X'] * 3 +
    ['Z>Y>X'] * 2
)


def household_id(n):
    return f'family-{n:02d}'


def applicant_id(n):
    return f'person-{n:02d}'


def application_events():
    events = []
    for n in range(1, 10):
        h = household_id(n)
        events.append({'id': f'G-A{n}', 'kind': 'household', 'household_id': h,
                       'community': 'ward-a', 'applied_at': '2026-04-01T09:00:00',
                       'residence_from': '2020-01-01T00:00:00', 'version': 1})
        events.append({'id': f'G-M{n}', 'kind': 'member', 'person_id': applicant_id(n),
                       'household_id': h, 'relation': 'applicant',
                       'joined_at': '2020-01-01T00:00:00'})
        for kind, minute in [('identity', 10), ('residence', 20), ('relation', 30)]:
            events.append({'id': f'G-D{n}-{kind}', 'kind': 'document', 'household_id': h,
                           'doc_kind': kind, 'subject': h,
                           'issued_at': '2026-03-01T00:00:00',
                           'received_at': f'2026-04-01T09:{minute:02d}:00',
                           'expires_at': '2027-03-01T00:00:00', 'issuer': 'civic-office'})
    return events


def eligibility_events():
    events = []
    for n in range(1, 10):
        h = household_id(n)
        for i, month in enumerate(('2026-01', '2026-02', '2026-03')):
            events.append({'id': f'G-I{n}-{i}', 'kind': 'income', 'household_id': h,
                           'month': month, 'cents': 100000, 'issuer': 'tax-office',
                           'at': '2026-04-01T10:00:00'})
        events.append({'id': f'G-P{n}', 'kind': 'property', 'household_id': h,
                       'area_m2': 0, 'effective_at': '2026-03-01T00:00:00',
                       'issuer': 'registry', 'at': '2026-04-01T10:00:00'})
    return events


def allocation_events(sign_overrides=None, omit_sign=()):
    sign_overrides = sign_overrides or {}
    events = []
    for n in range(1, 10):
        h = household_id(n)
        events.append({'id': f'G-U{n}', 'kind': 'unit', 'community': 'ward-a',
                       'rooms': 1, 'accessible': False,
                       'available_at': '2026-04-05T11:00:00'})
        events.append({'id': f'G-O{n}', 'kind': 'offer', 'household_id': h,
                       'unit_id': f'G-U{n}', 'offered_at': '2026-04-09T12:00:00',
                       'expires_at': '2026-05-05T12:00:00'})
        if n in omit_sign:
            continue
        events.append({'id': f'G-S{n}', 'kind': 'sign', 'offer_id': f'G-O{n}',
                       'at': sign_overrides.get(h, SIGN_AT), 'signer': applicant_id(n)})
    return events


def charter_event(charter_id, method):
    return {'id': charter_id, 'kind': 'charter', 'meeting_id': MEETING['id'],
            'contest': ['X', 'Y', 'Z'], 'roster_deadline': DEADLINE,
            'quorum': 7, 'method': method, 'tie_break': 'candidate_order',
            'published_at': PUBLISHED}


def ballot_events(start=10):
    events = []
    for i, ranking in enumerate(RANKING):
        n = i + 1
        events.append({'id': f'G-V{n}', 'kind': 'ballot', 'meeting_id': MEETING['id'],
                       'household_id': household_id(n),
                       'at': f'2026-05-0{2 + i % 5}T10:{i:02d}:00',
                       'ranked': ranking.split('>'), 'signer': applicant_id(n)})
    return events


def announcement():
    return {'id': 'G-ANNOUNCE', 'kind': 'announce', 'meeting_id': MEETING['id'], 'at': ANNOUNCE_AT}


def write(name, events):
    path = FIXTURES / name
    path.write_text(json.dumps({'events': events}, indent=1) + '\n')
    return path


PROXY_MEETING = {'id': 'MEET-PROXY', 'kind': 'meeting', 'community': 'ward-a',
                 'opens': '2026-05-01T00:00:00', 'closes': '2026-05-08T00:00:00',
                 'title': 'proxy review meeting'}
PROXY_CHARTER = {'id': 'CH-PROXY', 'kind': 'charter', 'meeting_id': 'MEET-PROXY',
                 'contest': ['X', 'Y', 'Z'], 'roster_deadline': DEADLINE,
                 'quorum': 3, 'method': 'plurality', 'tie_break': 'candidate_order',
                 'published_at': PUBLISHED}
PROXY_ANNOUNCE = {'id': 'P-ANNOUNCE', 'kind': 'announce', 'meeting_id': 'MEET-PROXY',
                  'at': ANNOUNCE_AT}


def proxy_duplicate_events():
    return [PROXY_MEETING, PROXY_CHARTER,
            {'id': 'P-A1', 'kind': 'authorize', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-01', 'authorizer': 'person-01', 'agent': 'person-02',
             'at': '2026-04-25T09:00:00', 'signature': 'written-proxy'},
            {'id': 'P-B1', 'kind': 'ballot', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-01', 'at': '2026-05-02T10:00:00',
             'ranked': ['X', 'Y', 'Z'], 'signer': 'person-01'},
            {'id': 'P-B2', 'kind': 'ballot', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-01', 'at': '2026-05-02T11:00:00',
             'ranked': ['Y', 'Z', 'X'], 'signer': 'person-02'},
            {'id': 'P-B3', 'kind': 'ballot', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-02', 'at': '2026-05-02T12:00:00',
             'ranked': ['Y', 'Z', 'X'], 'signer': 'person-02'},
            {'id': 'P-B4', 'kind': 'ballot', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-03', 'at': '2026-05-02T13:00:00',
             'ranked': ['Z', 'Y', 'X'], 'signer': 'person-03'},
            PROXY_ANNOUNCE]


def proxy_cycle_events():
    return [PROXY_MEETING, PROXY_CHARTER,
            {'id': 'C-A1', 'kind': 'authorize', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-01', 'authorizer': 'person-01', 'agent': 'person-02',
             'at': '2026-04-25T09:00:00', 'signature': 'w1'},
            {'id': 'C-A2', 'kind': 'authorize', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-02', 'authorizer': 'person-02', 'agent': 'person-01',
             'at': '2026-04-25T10:00:00', 'signature': 'w2'},
            {'id': 'C-B1', 'kind': 'ballot', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-01', 'at': '2026-05-02T10:00:00',
             'ranked': ['X', 'Y', 'Z'], 'signer': 'person-02'},
            {'id': 'C-B2', 'kind': 'ballot', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-02', 'at': '2026-05-02T11:00:00',
             'ranked': ['Y', 'Z', 'X'], 'signer': 'person-02'},
            {'id': 'C-B3', 'kind': 'ballot', 'meeting_id': 'MEET-PROXY',
             'household_id': 'family-03', 'at': '2026-05-02T12:00:00',
             'ranked': ['Z', 'Y', 'X'], 'signer': 'person-03'},
            {'id': 'C-ANNOUNCE', 'kind': 'announce', 'meeting_id': 'MEET-PROXY',
             'at': ANNOUNCE_AT}]


def withdrawal_events():
    return [{'id': 'G-W1', 'kind': 'withdrawal', 'meeting_id': MEETING['id'],
             'candidate': 'X', 'at': '2026-05-07T21:00:00'},
            {'id': 'G-ANNOUNCE-W', 'kind': 'announce', 'meeting_id': MEETING['id'],
             'at': '2026-05-07T22:00:00'}]


def correction_events():
    return [{'id': 'G-COR1', 'kind': 'correction', 'ballot_id': 'G-V5',
             'ranked': ['X', 'Y', 'Z'], 'at': '2026-05-07T21:00:00', 'signer': 'person-05'},
            {'id': 'G-ANNOUNCE-C', 'kind': 'announce', 'meeting_id': MEETING['id'],
             'at': '2026-05-07T22:00:00'}]


def main():
    write('governance-nine-application.json', application_events())
    write('governance-nine-eligibility.json', eligibility_events())
    write('governance-nine-allocation.json', allocation_events())

    common = [MEETING, charter_event('CH-PLURALITY', 'plurality')]
    write('governance-nine-plurality.json', common + ballot_events() + [announcement()])

    common_ir = [MEETING, charter_event('CH-IR', 'ir_elimination')]
    write('governance-nine-ir.json', common_ir + ballot_events() + [announcement()])

    # Late signature: family-09 signed on 2026-05-03, after the roster deadline,
    # but is imported as housed.  Its ballot must be excluded and the tie then
    # resolved by the pre-published candidate_order rule (X).
    late = allocation_events({'family-09': '2026-05-03T09:00:00'})
    write('governance-nine-late-sign-allocation.json', late)
    common_late = [MEETING, charter_event('CH-IR-LATE', 'ir_elimination')]
    write('governance-nine-late-sign.json', common_late + ballot_events() + [announcement()])

    write('governance-proxy-duplicate.json', proxy_duplicate_events())
    write('governance-proxy-cycle.json', proxy_cycle_events())
    # imported after governance-nine-plurality.json
    write('governance-nine-withdrawal.json', withdrawal_events())
    # imported after governance-nine-ir.json
    write('governance-nine-correction.json', correction_events())

    # Late-arriving evidence: family-09 has not signed when the first
    # announcement is made, so its ballot is held out.  The signature then
    # arrives with a real signing time before the roster deadline; a re-count
    # appends version 2 and leaves version 1 untouched.
    write('governance-nine-pending-sign-allocation.json', allocation_events(omit_sign=(9,)))
    write('governance-nine-pending-sign.json',
          [MEETING, charter_event('CH-IR-PENDING', 'ir_elimination')] + ballot_events() + [announcement()])
    write('governance-nine-sign-evidence.json',
          [{'id': 'G-S9', 'kind': 'sign', 'offer_id': 'G-O9',
            'at': SIGN_AT, 'signer': applicant_id(9)}])
    write('governance-nine-reannounce.json',
          [{'id': 'G-ANNOUNCE-2', 'kind': 'announce', 'meeting_id': MEETING['id'],
            'at': '2026-05-07T21:00:00'}])


if __name__ == '__main__':
    main()
