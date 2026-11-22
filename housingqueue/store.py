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
CREATE TABLE IF NOT EXISTS waste_drop(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,stream TEXT NOT NULL,mass_grams INTEGER NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS waste_decision(household_id TEXT PRIMARY KEY,accepted_grams INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reward_decision(household_id TEXT PRIMARY KEY,points INTEGER NOT NULL,state TEXT NOT NULL,verified_grams INTEGER NOT NULL DEFAULT 0,basis TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS recycling_container(id TEXT PRIMARY KEY,state TEXT NOT NULL,sealed_at TEXT NOT NULL,tare_grams INTEGER NOT NULL,gross_grams INTEGER,net_grams INTEGER,note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS recycling_lot_ledger(id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,container_id TEXT NOT NULL,household_id TEXT NOT NULL,stream TEXT NOT NULL,waste_drop_id TEXT,change_grams INTEGER NOT NULL,basis TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS recycling_shipment(id TEXT PRIMARY KEY,at TEXT NOT NULL,state TEXT NOT NULL,plant TEXT,note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS recycling_shipment_container(shipment_id TEXT NOT NULL,container_id TEXT NOT NULL,PRIMARY KEY(shipment_id,container_id));
CREATE TABLE IF NOT EXISTS recycling_contamination(id TEXT PRIMARY KEY,scope TEXT NOT NULL,container_id TEXT,waste_drop_id TEXT,contaminant_grams INTEGER NOT NULL,at TEXT NOT NULL,inspector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recycling_ratio(id TEXT PRIMARY KEY,stream TEXT NOT NULL,ratio_bp INTEGER NOT NULL,published_at TEXT NOT NULL,note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS processing_certificate(id TEXT PRIMARY KEY,shipment_id TEXT NOT NULL,container_id TEXT NOT NULL,route TEXT NOT NULL,recycled_grams INTEGER NOT NULL,residue_grams INTEGER NOT NULL,energy_grams INTEGER NOT NULL,loss_grams INTEGER NOT NULL,at TEXT NOT NULL,plant TEXT NOT NULL,state TEXT NOT NULL,reuse_reference TEXT,reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS reward_lot(id INTEGER PRIMARY KEY,household_id TEXT NOT NULL,certificate_id TEXT NOT NULL,container_id TEXT NOT NULL,grams INTEGER NOT NULL,points INTEGER NOT NULL,state TEXT NOT NULL,basis TEXT NOT NULL,event_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reward_payout(id TEXT PRIMARY KEY,household_id TEXT NOT NULL,points INTEGER NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reward_recovery(id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,household_id TEXT NOT NULL,points INTEGER NOT NULL,reason TEXT NOT NULL,UNIQUE(event_id,household_id));
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))
