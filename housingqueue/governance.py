from .store import one, log


def decide(db, meeting_id, event_id=None):
    rows = db.execute('SELECT choice,COUNT(*) AS n FROM resident_ballot WHERE meeting_id=? GROUP BY choice', (meeting_id,)).fetchall()
    totals = {row['choice']: row['n'] for row in rows}
    yes, no = totals.get('yes', 0), totals.get('no', 0)
    state = 'pending' if yes + no == 0 else 'passed' if yes > no else 'failed'
    db.execute('INSERT INTO meeting_decision(meeting_id,yes_votes,no_votes,state) VALUES(?,?,?,?) ON CONFLICT(meeting_id) DO UPDATE SET yes_votes=excluded.yes_votes,no_votes=excluded.no_votes,state=excluded.state',
               (meeting_id, yes, no, state))
    log(db, 'governance', meeting_id, state, 'one current ballot per housed household', event_id)
    return state


def meeting(db, e):
    if e['opens'] >= e['closes']:
        raise ValueError('meeting deadline must follow opening')
    db.execute('INSERT INTO resident_meeting(id,community,opens,closes,title) VALUES(?,?,?,?,?)',
               (e['id'], e['community'], e['opens'], e['closes'], e['title']))
    return decide(db, e['id'], e['id'])


def ballot(db, e):
    meeting = one(db, 'SELECT * FROM resident_meeting WHERE id=?', (e['meeting_id'],))
    household = one(db, 'SELECT * FROM household WHERE id=?', (e['household_id'],))
    applicant = one(db, "SELECT id FROM member WHERE household_id=? AND relation='applicant' AND state='active'", (e['household_id'],))
    if not meeting or not household or household['status'] not in ('housed', 'housed_disputed') or household['community'] != meeting['community']:
        raise ValueError('ballot requires housed household in meeting community')
    if not applicant or applicant['id'] != e['signer'] or not meeting['opens'] <= e['at'] <= meeting['closes']:
        raise ValueError('current applicant signature within meeting window required')
    if e['choice'] not in ('yes', 'no') or one(db, 'SELECT id FROM resident_ballot WHERE meeting_id=? AND household_id=?', (meeting['id'], household['id'])):
        raise ValueError('one yes/no ballot per household')
    db.execute('INSERT INTO resident_ballot(id,meeting_id,household_id,at,choice,signer) VALUES(?,?,?,?,?,?)',
               (e['id'], meeting['id'], household['id'], e['at'], e['choice'], e['signer']))
    return decide(db, meeting['id'], e['id'])
