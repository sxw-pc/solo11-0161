"""Community resident meetings and versioned electoral decisions.

Legacy meetings keep the original yes/no ballot behaviour.  A meeting that has
adopted a charter uses ranked ballots bound to the roster frozen at the
charter's roster deadline.  Every tally is announced as an immutable
``decision_version``; re-counts append a new version and never rewrite a
published one.
"""
import json

from . import allocation, application
from .store import one, many, log

METHODS = ('plurality', 'ir_elimination')
TIE_BREAKS = ('candidate_order',)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _ranked(value):
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith('['):
            items = json.loads(text)
        else:
            items = [x.strip() for x in text.replace('>', ',').split(',') if x.strip()]
    else:
        raise ValueError('ranked preferences must be a list or string')
    if not isinstance(items, list) or any(not isinstance(x, str) or not x for x in items):
        raise ValueError('ranked preferences must be non-empty strings')
    return list(items)


def _contest(value):
    if isinstance(value, str):
        value = [x.strip() for x in value.split(',') if x.strip()]
    if not isinstance(value, list) or not value:
        raise ValueError('charter requires a non-empty candidate contest')
    if any(not isinstance(x, str) or not x for x in value) or len(set(value)) != len(value):
        raise ValueError('candidates must be unique non-empty names')
    return list(value)


def _adopted(db, meeting_id):
    return one(db, '''SELECT c.*,m.adopted_at FROM meeting_charter m
                      JOIN election_charter c ON c.id=m.charter_id AND c.version=m.version
                      WHERE m.meeting_id=?''', (meeting_id,))


def _meeting(db, meeting_id):
    meeting = one(db, 'SELECT * FROM resident_meeting WHERE id=?', (meeting_id,))
    if not meeting:
        raise ValueError('unknown meeting')
    return meeting


def _household_community(db, person):
    row = one(db, 'SELECT h.community FROM member m JOIN household h ON h.id=m.household_id WHERE m.id=?', (person,))
    return row['community'] if row else None


# ---------------------------------------------------------------------------
# legacy yes/no decision (kept executable for the original governance-import)
# ---------------------------------------------------------------------------
def decide(db, meeting_id, event_id=None, at=None):
    meeting = _meeting(db, meeting_id)
    rows = db.execute('SELECT choice,COUNT(*) AS n FROM resident_ballot WHERE meeting_id=? GROUP BY choice', (meeting_id,)).fetchall()
    totals = {row['choice']: row['n'] for row in rows}
    yes, no = totals.get('yes', 0), totals.get('no', 0)
    state = 'pending' if yes + no == 0 else 'passed' if yes > no else 'failed'
    db.execute('INSERT INTO meeting_decision(meeting_id,yes_votes,no_votes,state) VALUES(?,?,?,?) ON CONFLICT(meeting_id) DO UPDATE SET yes_votes=excluded.yes_votes,no_votes=excluded.no_votes,state=excluded.state',
               (meeting_id, yes, no, state))
    prev = one(db, 'SELECT version FROM decision_version WHERE meeting_id=? ORDER BY version DESC LIMIT 1', (meeting_id,))
    version = 1 if not prev else prev['version'] + 1
    db.execute('''INSERT INTO decision_version(meeting_id,version,charter_id,method,quorum,roster_deadline,attendance,state,outcome,winner,tie_break,reason,supersedes,evidence,event_id,announced_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (meeting_id, version, 'legacy', 'yes_no', 0, meeting['closes'], yes + no, state, state, None, None,
                'legacy yes/no tally', prev['version'] if prev else None, _json({'yes': yes, 'no': no}), event_id,
                at or meeting['closes']))
    log(db, 'governance', meeting_id, state, 'legacy one yes/no ballot per housed household', event_id)
    return state


def meeting(db, e):
    if e['opens'] >= e['closes']:
        raise ValueError('meeting deadline must follow opening')
    db.execute('INSERT INTO resident_meeting(id,community,opens,closes,title) VALUES(?,?,?,?,?)',
               (e['id'], e['community'], e['opens'], e['closes'], e['title']))
    return 'scheduled'


# ---------------------------------------------------------------------------
# charter: published and locked before counting
# ---------------------------------------------------------------------------
def charter(db, e):
    meeting_row = _meeting(db, e['meeting_id'])
    if _adopted(db, meeting_row['id']):
        raise ValueError('charter already adopted and locked for meeting')
    if one(db, 'SELECT id FROM resident_ballot WHERE meeting_id=? LIMIT 1', (meeting_row['id'],)):
        raise ValueError('charter must be adopted before ballots are cast')
    contest = _contest(e.get('contest'))
    method = e.get('method', 'plurality')
    if method not in METHODS:
        raise ValueError('unsupported counting method')
    tie_break = e.get('tie_break', 'candidate_order')
    if tie_break not in TIE_BREAKS:
        raise ValueError('unsupported tie-break rule')
    deadline = e['roster_deadline']
    if not meeting_row['opens'] <= deadline <= meeting_row['closes']:
        raise ValueError('roster deadline must fall inside the meeting window')
    quorum = int(e['quorum'])
    if quorum < 0:
        raise ValueError('quorum cannot be negative')
    published_at = e['published_at']
    if published_at > meeting_row['closes']:
        raise ValueError('charter must be published before the meeting closes')
    db.execute('''INSERT INTO election_charter(id,meeting_id,version,contest,roster_deadline,quorum,method,tie_break,published_at,state)
                  VALUES(?,?,?,?,?,?,?,?,?,'locked')''',
               (e['id'], meeting_row['id'], 1, _json(contest), deadline, quorum, method, tie_break, published_at))
    db.execute('INSERT INTO meeting_charter(meeting_id,charter_id,version,adopted_at) VALUES(?,?,?,?)',
               (meeting_row['id'], e['id'], 1, published_at))
    log(db, 'governance', meeting_row['id'], 'charter_locked',
        f'{method} charter {e["id"]} published before counting', e['id'])
    return 'adopted'


def authorize(db, e):
    meeting_row = _meeting(db, e['meeting_id'])
    if not _adopted(db, meeting_row['id']):
        raise ValueError('proxy authorisation requires an adopted charter')
    if not one(db, 'SELECT id FROM household WHERE id=?', (e['household_id'],)):
        raise ValueError('unknown household')
    if not e.get('agent') or not e.get('authorizer') or not e.get('signature'):
        raise ValueError('written proxy authorisation requires agent, authorizer and signature')
    db.execute('INSERT INTO ballot_authorization(id,meeting_id,household_id,agent,authorizer,at,signature) VALUES(?,?,?,?,?,?,?)',
               (e['id'], meeting_row['id'], e['household_id'], e['agent'], e['authorizer'], e['at'], e['signature']))
    log(db, 'governance', meeting_row['id'], 'authorized', f'proxy {e["agent"]} for {e["household_id"]}', e['id'])
    return 'authorized'


def withdrawal(db, e):
    meeting_row = _meeting(db, e['meeting_id'])
    adopted = _adopted(db, meeting_row['id'])
    if not adopted:
        raise ValueError('candidate withdrawal requires an adopted charter')
    if e['candidate'] not in json.loads(adopted['contest']):
        raise ValueError('withdrawal names an unknown candidate')
    db.execute('INSERT INTO candidate_withdrawal(id,meeting_id,candidate,at) VALUES(?,?,?,?)',
               (e['id'], meeting_row['id'], e['candidate'], e['at']))
    log(db, 'governance', meeting_row['id'], 'withdrawn', f'candidate {e["candidate"]} withdrew', e['id'])
    return 'recorded'


# ---------------------------------------------------------------------------
# ballots
# ---------------------------------------------------------------------------
def _record_ranked(db, meeting_row, adopted, e):
    household = one(db, 'SELECT * FROM household WHERE id=?', (e['household_id'],))
    if not household:
        raise ValueError('unknown household')
    contest = json.loads(adopted['contest'])
    ranked = _ranked(e.get('ranked', e.get('choice')))
    if len(set(ranked)) != len(ranked):
        raise ValueError('ranked ballot repeats a candidate')
    if any(c not in contest for c in ranked):
        raise ValueError('ranked ballot names a candidate outside the charter')
    at = e['at']
    if at < adopted['published_at']:
        raise ValueError('ballot cannot predate charter publication')
    cast_as = 'self' if application.effective_applicant(db, household['id'], adopted['roster_deadline']) == e['signer'] else 'agent'
    db.execute('INSERT INTO resident_ballot(id,meeting_id,household_id,at,choice,signer,ranked,cast_as,agent) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'], meeting_row['id'], household['id'], at, 'abstain' if not ranked else '>'.join(ranked),
                e['signer'], _json(ranked), cast_as, e.get('agent')))
    reason = None
    if household['community'] != meeting_row['community']:
        reason = 'wrong_community'
    elif not meeting_row['opens'] <= at <= meeting_row['closes']:
        reason = 'outside_meeting_window'
    if reason:
        db.execute('INSERT INTO ballot_dispute(meeting_id,ballot_id,household_id,state,reason,event_id) VALUES(?,?,?,?,?,?)',
                   (meeting_row['id'], e['id'], household['id'], 'isolated', reason, e['id']))
    log(db, 'governance', meeting_row['id'], 'ballot_recorded', f'{household["id"]} ranked ballot', e['id'])
    return 'recorded'


def ballot(db, e):
    meeting_row = _meeting(db, e['meeting_id'])
    adopted = _adopted(db, meeting_row['id'])
    if adopted:
        return _record_ranked(db, meeting_row, adopted, e)
    # original yes/no path
    household = one(db, 'SELECT * FROM household WHERE id=?', (e['household_id'],))
    applicant = one(db, "SELECT id FROM member WHERE household_id=? AND relation='applicant' AND state='active'", (e['household_id'],))
    if not household or household['status'] not in ('housed', 'housed_disputed') or household['community'] != meeting_row['community']:
        raise ValueError('ballot requires housed household in meeting community')
    if not applicant or applicant['id'] != e['signer'] or not meeting_row['opens'] <= e['at'] <= meeting_row['closes']:
        raise ValueError('current applicant signature within meeting window required')
    if e['choice'] not in ('yes', 'no') or one(db, 'SELECT id FROM resident_ballot WHERE meeting_id=? AND household_id=?', (meeting_row['id'], household['id'])):
        raise ValueError('one yes/no ballot per household')
    db.execute('INSERT INTO resident_ballot(id,meeting_id,household_id,at,choice,signer,ranked,cast_as,agent) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'], meeting_row['id'], household['id'], e['at'], e['choice'], e['signer'], '[]', 'self', None))
    return decide(db, meeting_row['id'], e['id'], e['at'])


def correction(db, e):
    row = one(db, 'SELECT * FROM resident_ballot WHERE id=?', (e['ballot_id'],))
    if not row:
        raise ValueError('unknown ballot')
    adopted = _adopted(db, row['meeting_id'])
    if not adopted:
        raise ValueError('ballot correction requires an adopted charter')
    if e['signer'] != row['signer']:
        raise ValueError('correction must be signed by the original ballot signer')
    if e['at'] < row['at']:
        raise ValueError('correction cannot predate the ballot')
    ranked = _ranked(e.get('ranked'))
    contest = json.loads(adopted['contest'])
    if len(set(ranked)) != len(ranked) or any(c not in contest for c in ranked):
        raise ValueError('corrected ranking is outside the charter contest')
    db.execute('INSERT INTO ballot_correction(id,ballot_id,ranked,at,signer) VALUES(?,?,?,?,?)',
               (e['id'], row['id'], _json(ranked), e['at'], e['signer']))
    log(db, 'governance', row['meeting_id'], 'ballot_corrected', f'ballot {row["id"]} corrected', e['id'])
    return 'corrected'


# ---------------------------------------------------------------------------
# roster derived from allocation signature time and application identity
# ---------------------------------------------------------------------------
def _roster(db, meeting_row, adopted):
    deadline = adopted['roster_deadline']
    roster, status = {}, {}
    households = many(db, 'SELECT id,community,status FROM household ORDER BY id')
    for household in households:
        if household['community'] != meeting_row['community']:
            if one(db, 'SELECT id FROM resident_ballot WHERE meeting_id=? AND household_id=? LIMIT 1', (meeting_row['id'], household['id'])):
                status[household['id']] = 'wrong_community'
            continue
        # ``signature.at`` is the real signing instant recorded by the
        # allocation module, independent of when the governance file arrived.
        signed_at = allocation.signed_at(db, household['id'])
        if signed_at is None:
            status[household['id']] = 'no_signature_evidence'
            continue
        if signed_at > deadline:
            status[household['id']] = 'signed_after_roster_deadline'
            continue
        person = application.effective_applicant(db, household['id'], deadline)
        if not person:
            status[household['id']] = 'no_effective_applicant'
            continue
        roster[household['id']] = {'household_id': household['id'], 'person_id': person,
                                   'signed_at': signed_at, 'source': 'signed_by_roster_deadline'}
    # any ballot household not in this community
    for row in many(db, 'SELECT DISTINCT household_id FROM resident_ballot WHERE meeting_id=?', (meeting_row['id'],)):
        if row['household_id'] not in roster and row['household_id'] not in status:
            status[row['household_id']] = 'wrong_community'
    return roster, status


# ---------------------------------------------------------------------------
# proxy chains: cycles and out-of-window / cross-community edges are isolated
# ---------------------------------------------------------------------------
def _auth_graph(db, meeting_row, adopted):
    """Person-level authorisation graph for one meeting.

    An edge ``authorizer -> agent`` exists only for a written authorisation
    recorded on or before the roster deadline whose parties both belong to the
    meeting community.  The edge remembers the household on whose behalf the
    authorisation was given.  Invalid edges are kept aside so a disputed ballot
    can report the precise reason rather than being resolved by file arrival
    order.
    """
    deadline = adopted['roster_deadline']
    rows = many(db, 'SELECT * FROM ballot_authorization WHERE meeting_id=? ORDER BY at,id', (meeting_row['id'],))
    valid, all_edges, late, foreign = {}, {}, set(), set()
    for row in rows:
        key = (row['authorizer'], row['agent'])
        all_edges.setdefault(row['authorizer'], []).append((row['agent'], row['household_id']))
        if row['at'] > deadline:
            late.add(key)
            continue
        authorizer_community = _household_community(db, row['authorizer'])
        agent_community = _household_community(db, row['agent'])
        if (authorizer_community is not None and authorizer_community != meeting_row['community']) or \
           (agent_community is not None and agent_community != meeting_row['community']):
            foreign.add(key)
            continue
        valid.setdefault(row['authorizer'], []).append((row['agent'], row['household_id']))
    return {'valid': valid, 'all': all_edges, 'late': late, 'foreign': foreign}


def _has_cycle(edges, start):
    colour = {}

    def visit(node):
        colour[node] = 1
        for nxt, _ in edges.get(node, ()):
            if colour.get(nxt) == 1:
                return True
            if colour.get(nxt, 0) == 0 and visit(nxt):
                return True
        colour[node] = 2
        return False

    return visit(start)


def _path(edges, start, target, household_id=None):
    """Return the edge list of a path start -> target, or None.

    When ``household_id`` is given every edge must have been given on behalf of
    that household, so a route stitched together from other households'
    authorisations cannot legitimise a ballot.
    """
    visited = set()

    def walk(node):
        if node == target:
            return []
        if node in visited:
            return None
        visited.add(node)
        for nxt, edge_household in edges.get(node, ()):
            if household_id is not None and edge_household != household_id:
                continue
            rest = walk(nxt)
            if rest is not None:
                return [(node, nxt, edge_household)] + rest
        return None

    return walk(start)


def _signer_decision(db, meeting_row, graph, household_id, person, signer):
    """Classify how ``signer`` came to cast ``household_id``'s ballot."""
    if signer == person:
        return 'self', 'self ballot by the effective applicant', None
    edges = graph['valid']
    if _has_cycle(edges, person):
        return 'disputed', 'proxy authorisation chain contains a cycle', 'agent_cycle'
    path = _path(edges, person, signer, household_id)
    if path is not None:
        return 'agent', 'proxy ballot under written authorisation', None
    # no valid path: explain the dispute using the raw authorisation edges
    raw = _path(graph['all'], person, signer)
    if raw is not None:
        if any((a, b) in graph['late'] for a, b, _ in raw):
            return 'disputed', 'proxy authorisation signed after the roster deadline', 'authorization_expired'
        if any((a, b) in graph['foreign'] for a, b, _ in raw):
            return 'disputed', 'proxy authorisation crosses communities', 'agent_cross_community'
        if any(edge_household != household_id for _, _, edge_household in raw):
            return 'disputed', 'proxy delegation does not attach to this household', 'proxy_chain_cross_household'
    if _household_community(db, signer) not in (None, meeting_row['community']):
        return 'disputed', 'proxy signer does not belong to the meeting community', 'agent_cross_community'
    return 'disputed', 'signer is neither the effective applicant nor an authorised agent', 'unauthorized_signer'



# ---------------------------------------------------------------------------
# tally
# ---------------------------------------------------------------------------
def _plurality(contest, active, ballots):
    totals = {c: 0 for c in active}
    continuing = 0
    for ranked in ballots:
        for candidate in ranked:
            if candidate in active:
                totals[candidate] += 1
                continuing += 1
                break
    if not continuing:
        return None, [{'round_no': 1, 'totals': totals, 'transfers': {}, 'eliminated': None,
                       'reason': 'no continuing preference', 'continuing': 0}]
    best = max(totals.values())
    leaders = [c for c in active if totals[c] == best]
    if len(leaders) == 1:
        winner, reason = leaders[0], 'plurality of first preferences'
    else:
        winner = min(leaders, key=contest.index)
        reason = 'tie at the top resolved by pre-published candidate_order: ' + ','.join(leaders)
    return winner, [{'round_no': 1, 'totals': totals, 'transfers': {}, 'eliminated': None,
                     'reason': reason, 'continuing': continuing, 'tied': leaders if len(leaders) > 1 else []}]


def _ir_elimination(contest, active, ballots):
    active = list(active)
    rounds = []
    if not active:
        return None, [{'round_no': 1, 'totals': {}, 'transfers': {}, 'eliminated': None,
                       'reason': 'no candidate remains after withdrawals', 'continuing': 0}]
    round_no = 0
    while active:
        round_no += 1
        if round_no > len(contest) + 2:
            return None, rounds + [{'round_no': round_no, 'totals': {}, 'transfers': {}, 'eliminated': None,
                                    'reason': 'round limit reached', 'continuing': 0}]
        totals = {c: 0 for c in active}
        continuing = 0
        for ranked in ballots:
            for candidate in ranked:
                if candidate in active:
                    totals[candidate] += 1
                    continuing += 1
                    break
        if not continuing:
            rounds.append({'round_no': round_no, 'totals': totals, 'transfers': {}, 'eliminated': None,
                           'reason': 'all ballots exhausted', 'continuing': 0})
            return None, rounds
        if len(active) == 1:
            rounds.append({'round_no': round_no, 'totals': totals, 'transfers': {}, 'eliminated': None,
                           'reason': 'sole remaining candidate', 'continuing': continuing})
            return active[0], rounds
        best = max(totals.values())
        leaders = [c for c in active if totals[c] == best]
        if len(leaders) > 1 and len(leaders) == len(active):
            # every remaining candidate is tied for the lead: a tie for the
            # win, decided by the tie-break the charter published in advance.
            winner = min(leaders, key=contest.index)
            rounds.append({'round_no': round_no, 'totals': totals, 'transfers': {}, 'eliminated': None,
                           'reason': 'tie at the top resolved by pre-published candidate_order: ' + ','.join(leaders),
                           'continuing': continuing, 'tied': leaders})
            return winner, rounds
        if len(leaders) == 1 and best * 2 > continuing:
            rounds.append({'round_no': round_no, 'totals': totals, 'transfers': {}, 'eliminated': None,
                           'reason': 'majority of continuing ballots', 'continuing': continuing})
            return leaders[0], rounds
        low = min(totals.values())
        tied = [c for c in active if totals[c] == low]
        if len(tied) > 1:
            # candidate_order favours the earlier-listed candidate, so the
            # later-listed one is eliminated when the lowest count is tied.
            eliminated = max(tied, key=contest.index)
            reason = 'lowest-count tie resolved by pre-published candidate_order, eliminating ' + eliminated
        else:
            eliminated = tied[0]
            reason = 'lowest count eliminated'
        transfers = {}
        for ranked in ballots:
            remaining = [c for c in ranked if c in active]
            if remaining and remaining[0] == eliminated:
                nxt = remaining[1] if len(remaining) > 1 else 'exhausted'
                transfers[nxt] = transfers.get(nxt, 0) + 1
        rounds.append({'round_no': round_no, 'totals': totals, 'transfers': transfers,
                       'eliminated': eliminated, 'reason': reason, 'continuing': continuing,
                       'tied': tied if len(tied) > 1 else []})
        active.remove(eliminated)
    return None, rounds


def _resolve_ballots(db, meeting_row, adopted, roster, roster_status):
    contest = json.loads(adopted['contest'])
    published_at = adopted['published_at']
    corrections = {}
    for row in many(db, '''SELECT c.* FROM ballot_correction c JOIN resident_ballot b ON b.id=c.ballot_id
                           WHERE b.meeting_id=? ORDER BY c.at,c.id''', (meeting_row['id'],)):
        corrections[row['ballot_id']] = _ranked(row['ranked'])
    intake = {}
    for row in many(db, 'SELECT * FROM ballot_dispute WHERE meeting_id=? ORDER BY id', (meeting_row['id'],)):
        intake[row['ballot_id']] = row['reason']
    graph = _auth_graph(db, meeting_row, adopted)
    rows = many(db, 'SELECT * FROM resident_ballot WHERE meeting_id=? ORDER BY at,id', (meeting_row['id'],))
    resolved = []
    for row in rows:
        ranked = corrections.get(row['id'], _ranked(row['ranked']))
        item = {'ballot_id': row['id'], 'household_id': row['household_id'], 'at': row['at'],
                'signer': row['signer'], 'ranked': ranked, 'cast_as': 'self', 'agent': None,
                'status': 'counted', 'reason': 'valid ballot', 'counted': 1, 'round_applied': None}
        reason = intake.get(row['id'])
        if reason is None and ranked and not set(ranked).issubset(contest):
            reason = 'candidate_outside_charter'
        if reason is None and row['household_id'] not in roster:
            reason = roster_status.get(row['household_id'], 'household_not_on_roster')
        if reason is None and row['at'] < published_at:
            reason = 'charter_not_published'
        if reason is None and not (meeting_row['opens'] <= row['at'] <= meeting_row['closes']):
            reason = 'outside_meeting_window'
        if reason is None:
            person = roster[row['household_id']]['person_id']
            cast_as, detail, dispute = _signer_decision(db, meeting_row, graph, row['household_id'], person, row['signer'])
            item['cast_as'] = cast_as
            if cast_as == 'agent':
                item['agent'] = row['signer']
                item['reason'] = detail
            if dispute is not None:
                reason = dispute
        if reason is not None:
            item.update(status='disputed', reason=reason, counted=0)
        elif not ranked:
            item.update(status='abstain', reason='attendance only, no preference', counted=1)
        resolved.append(item)

    # one household cannot deliver both a self and a proxy vote, and no
    # arrival-order choice is made between competing proxy ballots.
    by_household = {}
    for item in resolved:
        by_household.setdefault(item['household_id'], []).append(item)
    for household_id, group in by_household.items():
        valid = [x for x in group if x['status'] in ('counted', 'abstain')]
        self_ballots = [x for x in valid if x['cast_as'] == 'self']
        proxy_ballots = [x for x in valid if x['cast_as'] == 'agent']
        if len(self_ballots) > 1:
            for x in self_ballots:
                x.update(status='disputed', reason='duplicate_self_ballot', counted=0)
        elif len(self_ballots) == 1:
            for x in proxy_ballots:
                x.update(status='duplicate_superseded', counted=0,
                         reason='self ballot takes precedence over proxy ballot')
        elif len(proxy_ballots) > 1:
            for x in proxy_ballots:
                x.update(status='disputed', reason='duplicate_agent_ballot', counted=0)
    return resolved


def _tally(db, meeting_row, adopted):
    contest = json.loads(adopted['contest'])
    method = adopted['method']
    roster, roster_status = _roster(db, meeting_row, adopted)
    resolved = _resolve_ballots(db, meeting_row, adopted, roster, roster_status)
    withdrawn = [r['candidate'] for r in many(db, 'SELECT candidate FROM candidate_withdrawal WHERE meeting_id=? ORDER BY at,id', (meeting_row['id'],))]
    active = [c for c in contest if c not in withdrawn]
    counted = [x for x in resolved if x['status'] in ('counted', 'abstain')]
    attendance = len(counted)
    pref_ballots = [x['ranked'] for x in counted if x['status'] == 'counted']
    quorum = adopted['quorum']
    if attendance < quorum:
        rounds = [{'round_no': 1, 'totals': {}, 'transfers': {}, 'eliminated': None,
                   'reason': f'attendance {attendance} below quorum {quorum}', 'continuing': 0}]
        result = {'outcome': 'no_quorum', 'winner': None, 'rounds': rounds, 'attendance': attendance,
                  'reason': f'attendance {attendance} is below the chartered quorum {quorum}'}
    elif method == 'plurality':
        winner, rounds = _plurality(contest, active, pref_ballots)
        result = {'outcome': 'winner' if winner else 'no_winner', 'winner': winner, 'rounds': rounds,
                  'attendance': attendance,
                  'reason': 'relative majority of first preferences' if winner else 'no continuing preference'}
    else:
        winner, rounds = _ir_elimination(contest, active, pref_ballots)
        result = {'outcome': 'winner' if winner else 'no_winner', 'winner': winner, 'rounds': rounds,
                  'attendance': attendance,
                  'reason': 'instant-runoff elimination of the lowest candidate each round' if winner else 'no winner after eliminations'}
    # record the round at which each counted ballot was first applied
    for item in resolved:
        if item['status'] in ('counted', 'abstain'):
            item['round_applied'] = 1
        else:
            item['round_applied'] = None
    result.update({'resolved': resolved, 'roster': roster, 'roster_status': roster_status,
                   'withdrawn': withdrawn, 'active': active, 'contest': contest, 'method': method})
    return result


def _same(prev, result):
    old_evidence = json.loads(prev['evidence'])
    old_ballots = {b['ballot_id']: (b['status'], b['counted'], b['ranked'])
                   for b in old_evidence.get('ballots', [])}
    new_ballots = {b['ballot_id']: (b['status'], b['counted'], b['ranked'])
                   for b in result['resolved']}
    old_roster = old_evidence.get('roster')
    new_roster = [result['roster'][h] for h in sorted(result['roster'])]
    return (prev['outcome'], prev['winner'], prev['attendance'],
            old_evidence.get('rounds'), old_roster, old_evidence.get('withdrawn'),
            old_ballots) == (result['outcome'], result['winner'], result['attendance'],
                             result['rounds'], new_roster, result['withdrawn'], new_ballots)


def announce(db, e):
    meeting_row = _meeting(db, e['meeting_id'])
    adopted = _adopted(db, meeting_row['id'])
    if not adopted:
        return decide(db, meeting_row['id'], e['id'], e.get('at') or meeting_row['closes'])
    at = e.get('at') or meeting_row['closes']
    if not meeting_row['opens'] <= at <= meeting_row['closes']:
        raise ValueError('announcement must fall inside the meeting window')
    if at < adopted['published_at']:
        raise ValueError('charter must be published before an announcement')
    result = _tally(db, meeting_row, adopted)
    prev = one(db, 'SELECT * FROM decision_version WHERE meeting_id=? ORDER BY version DESC LIMIT 1', (meeting_row['id'],))
    if prev and _same(prev, result):
        log(db, 'governance', meeting_row['id'], 'announcement_unchanged', 'recount matches published result', e['id'])
        return 'unchanged'
    version = 1 if not prev else prev['version'] + 1
    prev_ballots = {}
    prev_evidence = {}
    if prev:
        prev_evidence = json.loads(prev['evidence'])
        prev_ballots = {b['ballot_id']: b for b in prev_evidence.get('ballots', [])}
    changed = []
    withdrew = []
    if prev:
        for item in result['resolved']:
            old = prev_ballots.get(item['ballot_id'])
            old_status = old['status'] if old else None
            old_counted = old['counted'] if old else 0
            old_ranked = old['ranked'] if old else None
            if (old_status, old_counted, old_ranked) != (item['status'], item['counted'], item['ranked']):
                changed.append(item['ballot_id'])
        # A candidate withdrawal changes the ranking without any single ballot
        # changing status; record the withdrawal and every ballot that ranked
        # the withdrawn candidate as the evidence behind the new version.
        withdrew = sorted(set(result['withdrawn']) - set(prev_evidence.get('withdrawn', [])))
        for item in result['resolved']:
            if item['ballot_id'] not in changed and any(c in item['ranked'] for c in withdrew):
                changed.append(item['ballot_id'])
    else:
        withdrew = sorted(result['withdrawn'])
    runner_up = None
    old_rounds = prev_evidence.get('rounds') or []
    if prev and prev['winner'] != result['winner'] and old_rounds:
        last = old_rounds[-1]
        totals = last.get('totals') or {}
        others = sorted(((v, k) for k, v in totals.items() if k != prev['winner']), reverse=True)
        runner_up = others[0][1] if others else None
    evidence = {
        'charter': {'id': adopted['id'], 'version': adopted['version'], 'method': adopted['method'],
                    'tie_break': adopted['tie_break'], 'quorum': adopted['quorum'],
                    'roster_deadline': adopted['roster_deadline'], 'contest': result['contest']},
        'withdrawn': result['withdrawn'], 'active': result['active'],
        'roster': [result['roster'][h] for h in sorted(result['roster'])],
        'rounds': result['rounds'],
        'ballots': [{'ballot_id': x['ballot_id'], 'household_id': x['household_id'],
                     'status': x['status'], 'reason': x['reason'], 'counted': x['counted'],
                     'ranked': x['ranked'], 'cast_as': x['cast_as'], 'round_applied': x['round_applied']}
                    for x in result['resolved']],
        'changed_ballots': changed,
        'withdrawals_added': withdrew,
        'previous_winner': prev['winner'] if prev else None,
        'previous_ranking': [],
        'winner_changed': bool(prev and prev['winner'] != result['winner']),
        'runner_up_before': runner_up,
        'quorum': adopted['quorum'], 'attendance': result['attendance'],
    }
    # The ranking of candidates, most votes first, in the final round of each
    # version.  A re-count that changes the order lists the ballots responsible.
    def ranking(rounds):
        if not rounds:
            return []
        totals = rounds[-1].get('totals') or {}
        return [c for c, _ in sorted(totals.items(), key=lambda kv: (-kv[1], result['contest'].index(kv[0])))]
    evidence['ranking'] = ranking(result['rounds'])
    evidence['previous_ranking'] = ranking(old_rounds)
    db.execute('''INSERT INTO decision_version(meeting_id,version,charter_id,method,quorum,roster_deadline,attendance,state,outcome,winner,tie_break,reason,supersedes,evidence,event_id,announced_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (meeting_row['id'], version, adopted['id'], adopted['method'], adopted['quorum'],
                adopted['roster_deadline'], result['attendance'], 'announced', result['outcome'],
                result['winner'], adopted['tie_break'], result['reason'],
                prev['version'] if prev else None, _json(evidence), e['id'], at))
    snapshot_version = version
    for household_id in sorted(result['roster']):
        entry = result['roster'][household_id]
        db.execute('INSERT INTO roster_snapshot(meeting_id,version,household_id,person_id,signed_at,community,source) VALUES(?,?,?,?,?,?,?)',
                   (meeting_row['id'], snapshot_version, household_id, entry['person_id'], entry['signed_at'],
                    meeting_row['community'], entry['source']))
    for item in result['resolved']:
        db.execute('''INSERT INTO decision_ballot(meeting_id,version,ballot_id,household_id,status,reason,counted,round_applied)
                      VALUES(?,?,?,?,?,?,?,?)''',
                   (meeting_row['id'], version, item['ballot_id'], item['household_id'], item['status'],
                    item['reason'], item['counted'], item['round_applied']))
    for row in result['rounds']:
        db.execute('''INSERT INTO decision_round(meeting_id,version,round_no,totals,transfers,eliminated,reason)
                      VALUES(?,?,?,?,?,?,?)''',
                   (meeting_row['id'], version, row['round_no'], _json(row['totals']),
                    _json(row['transfers']), row.get('eliminated'), row['reason']))
    for ballot_id in changed:
        item = next(x for x in result['resolved'] if x['ballot_id'] == ballot_id)
        old = prev_ballots.get(ballot_id)
        db.execute('''INSERT INTO decision_delta(meeting_id,version,ballot_id,previous_status,current_status,previous_counted,current_counted,rank_moved)
                      VALUES(?,?,?,?,?,?,?,?)''',
                   (meeting_row['id'], version, ballot_id,
                    old['status'] if old else None, item['status'],
                    old['counted'] if old else 0, item['counted'],
                    _json({'previous': old['ranked'] if old else None, 'current': item['ranked']})))
    log(db, 'governance', meeting_row['id'], result['outcome'],
        f'version {version} {adopted["method"]} winner={result["winner"]}', e['id'])
    return result['winner'] if result['outcome'] == 'winner' else result['outcome']
