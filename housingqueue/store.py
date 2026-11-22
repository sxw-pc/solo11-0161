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
CREATE TABLE IF NOT EXISTS water_tap(id TEXT PRIMARY KEY,unit_id TEXT NOT NULL,zone TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS water_sample(id TEXT PRIMARY KEY,tap_id TEXT NOT NULL,sampled_at TEXT NOT NULL,cfu_per_litre INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_plan(id TEXT PRIMARY KEY,building TEXT NOT NULL,horizon INTEGER NOT NULL,threshold INTEGER NOT NULL,title TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_node(id TEXT PRIMARY KEY,building TEXT NOT NULL,kind TEXT NOT NULL,upstream_id,downstream_id,tap_id,initial_volume REAL,initial_cfu REAL,capacity REAL,cfu REAL,clean_inflow REAL,pipe_volume REAL,valve_state TEXT,demand REAL);
CREATE TABLE IF NOT EXISTS flow_action(id TEXT PRIMARY KEY,building TEXT NOT NULL,at_minute INTEGER NOT NULL,target_id TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,approved INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_sample(id TEXT PRIMARY KEY,tap_node_id TEXT NOT NULL,sampled_minute INTEGER NOT NULL,received_minute INTEGER NOT NULL,cfu_per_litre REAL NOT NULL,qc TEXT NOT NULL,threshold INTEGER NOT NULL,state TEXT NOT NULL,note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_minute(id INTEGER PRIMARY KEY,building TEXT NOT NULL,minute INTEGER NOT NULL,node_id TEXT NOT NULL,kind TEXT NOT NULL,volume REAL NOT NULL,cfu REAL,inflow REAL NOT NULL,outflow REAL NOT NULL,mass_in REAL NOT NULL,mass_out REAL NOT NULL,conservation_diff REAL NOT NULL,pump_on INTEGER NOT NULL,valve_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_parcel(id INTEGER PRIMARY KEY,building TEXT NOT NULL,minute INTEGER NOT NULL,parcel_uid TEXT NOT NULL,born_minute INTEGER NOT NULL,origin TEXT NOT NULL,volume REAL NOT NULL,cfu REAL NOT NULL,location TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_directive(id INTEGER PRIMARY KEY,building TEXT NOT NULL,minute INTEGER NOT NULL,tap_node_id TEXT NOT NULL,unit_id TEXT,directive TEXT NOT NULL,sim_cfu REAL,supply TEXT NOT NULL,basis TEXT NOT NULL,notify_target TEXT NOT NULL,notify_kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))
