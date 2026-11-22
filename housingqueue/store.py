import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS household(id TEXT PRIMARY KEY,community TEXT NOT NULL,applied_at TEXT NOT NULL,residence_from TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS member(id TEXT NOT NULL,household_id TEXT NOT NULL,relation TEXT NOT NULL,joined_at TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(id,household_id));
CREATE TABLE IF NOT EXISTS document(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,kind TEXT NOT NULL,subject TEXT NOT NULL,issued_at TEXT NOT NULL,received_at TEXT NOT NULL,expires_at TEXT NOT NULL,issuer TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS application_decision(id INTEGER PRIMARY KEY,household_id TEXT NOT NULL,state TEXT NOT NULL,accepted_at TEXT,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS income_check(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,month TEXT NOT NULL,cents INTEGER NOT NULL,issuer TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS property_check(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,area_m2 REAL NOT NULL,effective_at TEXT NOT NULL,issuer TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objection(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,opened_at TEXT NOT NULL,closes_at TEXT NOT NULL,reason TEXT NOT NULL,resolved_at TEXT,reviewer TEXT);
CREATE TABLE IF NOT EXISTS eligibility(id INTEGER PRIMARY KEY,household_id TEXT NOT NULL,state TEXT NOT NULL,priority INTEGER NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS queue_position(id INTEGER PRIMARY KEY,household_id TEXT NOT NULL,rank INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unit(id TEXT PRIMARY KEY,community TEXT NOT NULL,rooms INTEGER NOT NULL,accessible INTEGER NOT NULL,available_at TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,unit_id TEXT NOT NULL,offered_at TEXT NOT NULL,expires_at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,eligibility_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS signature(id TEXT PRIMARY KEY,offer_id TEXT NOT NULL,at TEXT NOT NULL,signer TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocation_dispute(id INTEGER PRIMARY KEY,offer_id TEXT NOT NULL,household_id TEXT NOT NULL,eligibility_version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);

-- Electoral charter: published and locked before ballots are counted.
-- contest=candidate list, roster_deadline, quorum, method (plurality|ir_elimination), tie_break.
CREATE TABLE IF NOT EXISTS election_charter(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL,version INTEGER NOT NULL,contest TEXT NOT NULL,roster_deadline TEXT NOT NULL,quorum INTEGER NOT NULL,method TEXT NOT NULL,tie_break TEXT NOT NULL,published_at TEXT NOT NULL,state TEXT NOT NULL);
-- Charter version adopted for each meeting; the adopted version is immutable once locked.
CREATE TABLE IF NOT EXISTS meeting_charter(meeting_id TEXT PRIMARY KEY,charter_id TEXT NOT NULL,version INTEGER NOT NULL,adopted_at TEXT NOT NULL);
-- At deadline the effective applicant and the roster snapshot are frozen.
CREATE TABLE IF NOT EXISTS roster_snapshot(meeting_id TEXT NOT NULL,version INTEGER NOT NULL,household_id TEXT NOT NULL,person_id TEXT,signed_at TEXT,community TEXT,source TEXT NOT NULL,PRIMARY KEY(meeting_id,version,household_id));
-- Written authorisation for an agent to act for one household at one meeting.
CREATE TABLE IF NOT EXISTS ballot_authorization(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL,household_id TEXT NOT NULL,agent TEXT NOT NULL,authorizer TEXT NOT NULL,at TEXT NOT NULL,signature TEXT NOT NULL);
-- Candidate withdrawal.  Withdrawal after an announcement must not rewrite it.
CREATE TABLE IF NOT EXISTS candidate_withdrawal(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL,candidate TEXT NOT NULL,at TEXT NOT NULL);
-- A ballot correction supersedes the ranking recorded on the ballot.
CREATE TABLE IF NOT EXISTS ballot_correction(id TEXT PRIMARY KEY,ballot_id TEXT NOT NULL,ranked TEXT NOT NULL,at TEXT NOT NULL,signer TEXT NOT NULL);
-- Ballots are ranked preference lists bound to a household; one per household per meeting.
CREATE TABLE IF NOT EXISTS resident_ballot(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL,household_id TEXT NOT NULL,at TEXT NOT NULL,choice TEXT NOT NULL,signer TEXT NOT NULL,ranked TEXT NOT NULL,cast_as TEXT NOT NULL,agent TEXT);
-- A ballot held out of the count pending review (duplicate/agent cycle/out of window/wrong community...).
CREATE TABLE IF NOT EXISTS ballot_dispute(id INTEGER PRIMARY KEY,meeting_id TEXT NOT NULL,ballot_id TEXT NOT NULL,household_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT);
-- Immutable tally announcements. Re-counts append new versions; published versions are never rewritten.
CREATE TABLE IF NOT EXISTS decision_version(meeting_id TEXT NOT NULL,version INTEGER NOT NULL,charter_id TEXT NOT NULL,method TEXT NOT NULL,quorum INTEGER NOT NULL,roster_deadline TEXT NOT NULL,attendance INTEGER NOT NULL,state TEXT NOT NULL,outcome TEXT NOT NULL,winner TEXT,tie_break TEXT,reason TEXT NOT NULL,supersedes INTEGER,evidence TEXT NOT NULL,event_id TEXT,announced_at TEXT,PRIMARY KEY(meeting_id,version));
CREATE TABLE IF NOT EXISTS decision_round(meeting_id TEXT NOT NULL,version INTEGER NOT NULL,round_no INTEGER NOT NULL,totals TEXT NOT NULL,transfers TEXT NOT NULL,eliminated TEXT,reason TEXT NOT NULL,PRIMARY KEY(meeting_id,version,round_no));
-- Per-ballot effect register: which ballots entered the count and their weight in each round.
CREATE TABLE IF NOT EXISTS decision_ballot(meeting_id TEXT NOT NULL,version INTEGER NOT NULL,ballot_id TEXT NOT NULL,household_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,counted INTEGER NOT NULL,round_applied INTEGER,PRIMARY KEY(meeting_id,version,ballot_id));
-- Which ballots differ from a previous decision version, i.e. the evidence behind a re-count.
CREATE TABLE IF NOT EXISTS decision_delta(meeting_id TEXT NOT NULL,version INTEGER NOT NULL,ballot_id TEXT NOT NULL,previous_status TEXT,current_status TEXT,previous_counted INTEGER NOT NULL,current_counted INTEGER NOT NULL,rank_moved TEXT,PRIMARY KEY(meeting_id,version,ballot_id));

CREATE TABLE IF NOT EXISTS resident_meeting(id TEXT PRIMARY KEY,community TEXT NOT NULL,opens TEXT NOT NULL,closes TEXT NOT NULL,title TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meeting_decision(meeting_id TEXT PRIMARY KEY,yes_votes INTEGER NOT NULL,no_votes INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
-- An announced version is a public record: it can never be rewritten or removed.
CREATE TRIGGER IF NOT EXISTS decision_version_immutable UPDATE ON decision_version WHEN OLD.state='announced'
BEGIN SELECT RAISE(ABORT,'announced decision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS decision_version_keep DELETE ON decision_version WHEN OLD.state='announced'
BEGIN SELECT RAISE(ABORT,'announced decision is immutable'); END;
-- Charters are locked once adopted.
CREATE TRIGGER IF NOT EXISTS election_charter_locked UPDATE ON election_charter
BEGIN SELECT RAISE(ABORT,'charter is locked once adopted'); END;
CREATE TRIGGER IF NOT EXISTS meeting_charter_locked UPDATE ON meeting_charter
BEGIN SELECT RAISE(ABORT,'charter is locked once adopted'); END;
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))
