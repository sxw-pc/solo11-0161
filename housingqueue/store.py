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
CREATE TABLE IF NOT EXISTS power_reading(id TEXT PRIMARY KEY,unit_id TEXT NOT NULL,at TEXT NOT NULL,kw_tenths INTEGER NOT NULL,meter TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_demand(community TEXT PRIMARY KEY,total_kw_tenths INTEGER NOT NULL,reading_count INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS battery(id TEXT PRIMARY KEY,community TEXT NOT NULL,usable_ws INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inverter(id TEXT PRIMARY KEY,community TEXT NOT NULL,limit_w INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grid_event(id TEXT PRIMARY KEY,community TEXT NOT NULL,at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device(id TEXT PRIMARY KEY,unit_id TEXT NOT NULL,label TEXT NOT NULL,critical INTEGER NOT NULL,power_w INTEGER NOT NULL,interruptible INTEGER NOT NULL,consent TEXT NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery(id TEXT PRIMARY KEY,offer_id TEXT NOT NULL,unit_id TEXT NOT NULL,household_id TEXT NOT NULL,at TEXT NOT NULL,witness TEXT NOT NULL,evidence TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_window(id TEXT PRIMARY KEY,community TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,slice_minutes INTEGER NOT NULL,adopt_seq INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supply_plan(id TEXT PRIMARY KEY,community TEXT NOT NULL,window_id TEXT NOT NULL,seq INTEGER NOT NULL,adopted INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supply_segment(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,segment_no INTEGER NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,battery_before_ws INTEGER NOT NULL,battery_after_ws INTEGER NOT NULL,power_w INTEGER NOT NULL,limit_w INTEGER NOT NULL,powered_devices TEXT NOT NULL,unfulfilled TEXT NOT NULL,state TEXT NOT NULL,witness_id TEXT,UNIQUE(plan_id,segment_no));
CREATE TABLE IF NOT EXISTS switch_witness(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,segment_no INTEGER NOT NULL,at TEXT NOT NULL,witness TEXT NOT NULL,evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))
