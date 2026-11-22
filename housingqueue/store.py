import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS household(id TEXT PRIMARY KEY,community TEXT NOT NULL,applied_at TEXT NOT NULL,residence_from TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS member(id TEXT NOT NULL,household_id TEXT NOT NULL,relation TEXT NOT NULL,joined_at TEXT NOT NULL,state TEXT NOT NULL,removed_at TEXT,PRIMARY KEY(id,household_id));
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
CREATE TABLE IF NOT EXISTS facility_room(id TEXT PRIMARY KEY,community TEXT NOT NULL,capacity INTEGER NOT NULL,accessible INTEGER NOT NULL,noise TEXT NOT NULL DEFAULT 'normal',equipment TEXT NOT NULL DEFAULT '[]',dividable INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS facility_area(id TEXT NOT NULL,room_id TEXT NOT NULL,capacity INTEGER NOT NULL,accessible INTEGER NOT NULL,noise TEXT NOT NULL,equipment TEXT NOT NULL,PRIMARY KEY(id,room_id));
CREATE TABLE IF NOT EXISTS facility_booking(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,room_id TEXT NOT NULL,starts TEXT NOT NULL,ends TEXT NOT NULL,attendees INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS facility_decision(booking_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,community TEXT NOT NULL,title TEXT NOT NULL,attendees INTEGER NOT NULL,starts TEXT NOT NULL,ends TEXT NOT NULL,noise TEXT NOT NULL,equipment TEXT NOT NULL,accessible INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,outcome TEXT NOT NULL,outcome_session_id TEXT);
CREATE TABLE IF NOT EXISTS activity_window(id TEXT PRIMARY KEY,activity_id TEXT NOT NULL,starts TEXT NOT NULL,ends TEXT NOT NULL,attendees INTEGER NOT NULL,consented_at TEXT NOT NULL,source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS guest_pass(id TEXT PRIMARY KEY,activity_id TEXT NOT NULL,person_id TEXT NOT NULL,issuer TEXT NOT NULL,at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facility_session(id TEXT PRIMARY KEY,activity_id TEXT NOT NULL,room_id TEXT NOT NULL,area_id TEXT,starts TEXT NOT NULL,ends TEXT NOT NULL,attendees INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,needs_equipment TEXT NOT NULL,room_equipment TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facility_room_use(id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,room_id TEXT NOT NULL,area_id TEXT,starts TEXT NOT NULL,ends TEXT NOT NULL,state TEXT NOT NULL,released_at TEXT);
CREATE TABLE IF NOT EXISTS facility_checkin(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,person_id TEXT NOT NULL,at TEXT NOT NULL,witness TEXT NOT NULL,witness_kind TEXT NOT NULL,source TEXT,late INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))
